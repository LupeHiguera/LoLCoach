import io
import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from coach import clips
from fetch_matches import SCHEMA
from recorder import SCHEMA as RECORDING_SCHEMA


def death(time):
    return dict(time=time, me='death', side='enemy', killer='Akali', victim='Ahri',
                assists=[], x=5000, y=6000)


MATCH = dict(match_id='NA1_123', patch='test', my_champion='Ahri',
             opp_champion='Akali', duration_s=600)


def populate(conn):
    conn.executescript(SCHEMA)
    conn.execute("""INSERT INTO matches (match_id, duration_s, my_participant_id,
        my_champion, opp_champion, raw_json) VALUES ('NA1_123',600,1,'Ahri','Akali',?)""",
        (json.dumps({'name': 'private-player', 'puuid': 'private-puuid'}),))
    conn.execute("INSERT INTO timelines VALUES ('NA1_123',60000,'{}')")
    conn.executemany("INSERT INTO participants (match_id, participant_id, champion, team_id, puuid) "
                     "VALUES ('NA1_123',?,?,?,?)",
                     [(1, 'Ahri', 100, 'my-private-puuid'), (6, 'Akali', 200, 'private-puuid')])
    conn.execute("""INSERT INTO events (match_id, timestamp_ms, type, killer_id, victim_id,
        raw_json) VALUES ('NA1_123',300000,'CHAMPION_KILL',6,1,?)""",
        ('{"summonerName":"private-player"}',))
    conn.commit()


class ClipPlanTests(unittest.TestCase):
    def test_signed_offset_and_relative_evidence(self):
        event = dict(death(250000), me='kill')
        manifest = clips.plan_clips(MATCH, [event, death(300000)], -90, 600)
        clip, = manifest['clips']
        self.assertEqual((clip['source_start_s'], clip['source_end_s']), (150, 220))
        self.assertEqual(clip['death_clip_s'], 60)
        self.assertEqual([e['clip_s'] for e in clip['events']], [10, 60])
        self.assertFalse(manifest['sync']['verified'])

    def test_partial_footage_keeps_actual_context_times(self):
        manifest = clips.plan_clips(MATCH, [death(100000), death(300000)], -90, 215)
        first, second = manifest['clips']
        self.assertTrue(first['truncated_before'])
        self.assertEqual((first['game_start_ms'], first['death_clip_s']), (90000, 10))
        self.assertTrue(second['truncated_after'])
        self.assertEqual(second['game_end_ms'], 305000)

    def test_deaths_outside_recording_are_skipped_not_clamped(self):
        manifest = clips.plan_clips(MATCH, [death(10000), death(300000)], -20, 280)
        self.assertEqual(manifest['clips'], [])
        self.assertEqual(len(manifest['skipped']), 2)

    def test_opening_and_match_end_boundaries(self):
        first, last = clips.plan_clips(MATCH, [death(10000), death(598000)], 5, 700)['clips']
        self.assertEqual((first['source_start_s'], first['death_clip_s']), (5, 10))
        self.assertEqual(last['game_end_ms'], 600000)
        self.assertFalse(last['truncated_after'])

    def test_invalid_sync_and_window_values_rejected(self):
        for kwargs in [dict(offset_s=float('nan')), dict(duration_s=float('inf')),
                       dict(before_s=-1), dict(after_s=float('nan')),
                       dict(before_s=0, after_s=0)]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                clips.plan_clips(MATCH, [death(300000)], **(dict(offset_s=0, duration_s=600) | kwargs))

    def test_no_deaths_is_a_valid_empty_batch(self):
        self.assertEqual(clips.plan_clips(MATCH, [], 0, 600)['clips'], [])


class EvidenceTests(unittest.TestCase):
    def test_whitelist_and_missing_timeline(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            populate(conn)
            match, events = clips.match_evidence(conn, 'NA1_123')
            manifest = clips.plan_clips(match, events, 0, 600)
            self.assertNotIn('private', json.dumps(manifest))
            self.assertEqual(events[0]['victim'], 'Ahri')
            conn.execute('DELETE FROM timelines')
            with self.assertRaisesRegex(ValueError, 'no timeline'):
                clips.match_evidence(conn, 'NA1_123')
        finally:
            conn.close()

    def test_only_linked_finished_recordings_used(self):
        with tempfile.TemporaryDirectory() as temp:
            notes = Path(temp) / 'reviews.db'
            with sqlite3.connect(notes) as conn:
                conn.executescript(RECORDING_SCHEMA)
                conn.execute("INSERT INTO recordings (match_id,path,offset_s,status) "
                             "VALUES ('NA1_123','finished.mp4',-5,'linked')")
                conn.execute("INSERT INTO recordings (match_id,path,offset_s,status) "
                             "VALUES ('NA1_123','unfinished.mp4',0,'recording')")
            self.assertEqual(clips.linked_video(notes, 'NA1_123'), (Path('finished.mp4').resolve(), -5))


class ExtractionTests(unittest.TestCase):
    def test_failed_batch_is_not_published_and_existing_batch_survives(self):
        manifest = clips.plan_clips(MATCH, [death(100000), death(300000)], 0, 600)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp)
            def fail_second(command):
                target = Path(command[-1])
                if target.name == 'death-300000.mp4':
                    raise ValueError('decode failed')
                target.write_bytes(b'clip')
            with mock.patch.object(clips, 'run_media', side_effect=fail_second):
                with self.assertRaisesRegex(ValueError, 'decode failed'):
                    clips.extract_clips(manifest, Path('source.mp4'), output)
            self.assertEqual(list(output.iterdir()), [])
            existing = output / MATCH['match_id']
            existing.mkdir()
            sentinel = existing / 'manifest.json'
            sentinel.write_text('previous review')
            with self.assertRaisesRegex(ValueError, 'already exists'):
                clips.extract_clips(manifest, Path('source.mp4'), output)
            self.assertEqual(sentinel.read_text(), 'previous review')

    def test_output_cannot_escape_root(self):
        manifest = clips.plan_clips(dict(MATCH, match_id='../escape'), [], 0, 600)
        with tempfile.TemporaryDirectory() as temp, self.assertRaisesRegex(ValueError, 'safe output'):
            clips.extract_clips(manifest, Path('source.mp4'), Path(temp))

    def test_dry_run_never_modifies_db_or_writes_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, video, output = root / 'league.db', root / 'video.mp4', root / 'clips'
            with sqlite3.connect(db) as conn:
                populate(conn)
            original = db.read_bytes()
            video.write_bytes(b'fixture')
            stdout = io.StringIO()
            with mock.patch.object(clips, 'video_duration', return_value=600), redirect_stdout(stdout):
                clips.main(['--match', 'NA1_123', '--db', str(db), '--video', str(video),
                            '--offset', '-5', '--output', str(output), '--dry-run'])
            self.assertEqual(db.read_bytes(), original)
            self.assertFalse(output.exists())
            self.assertEqual(json.loads(stdout.getvalue())['sync']['source'], 'manual')

    def test_missing_tools_have_actionable_error(self):
        with mock.patch.object(clips.subprocess, 'run', side_effect=FileNotFoundError):
            with self.assertRaisesRegex(ValueError, 'Install FFmpeg'):
                clips.video_duration(Path('source.mp4'))

    def test_no_video_stream_and_invalid_duration_rejected(self):
        for result in [dict(streams=[]), dict(streams=[dict(duration='NaN')])]:
            with mock.patch.object(clips, 'run_media', return_value=json.dumps(result)):
                with self.assertRaises(ValueError):
                    clips.video_duration(Path('source.mp4'))

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg not installed')
    def test_real_video_extracts_playable_clip_with_expected_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            video = root / 'source.mp4'
            clips.run_media(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                             '-f', 'lavfi', '-i', 'testsrc=size=160x120:rate=10', '-t', '8',
                             '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)])
            manifest = clips.plan_clips(dict(MATCH, duration_s=8), [death(4000)],
                                        0, clips.video_duration(video), 2, 1)
            destination = clips.extract_clips(manifest, video, root / 'output')
            self.assertAlmostEqual(clips.video_duration(destination / 'death-4000.mp4'), 3, delta=0.15)
            self.assertEqual(json.loads((destination / 'manifest.json').read_text()), manifest)


if __name__ == '__main__':
    unittest.main()
