"""Regression: sanitised coach, eval, clip, and export payloads drop identifiers."""
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path

from coach import cloud, clips, eval as evaluation, harness, privacy
from export_demo import build
from fetch_matches import SCHEMA, store_match, store_timeline
from test_clips import MATCH, death, populate
from test_export_demo import ME, SECRETS, riot_match, timeline

EXAMPLES = Path(__file__).parent / 'coach' / 'examples'
TOKENS = (
    'player#NA1',
    '/Users/alice/recording.mp4',
    '/opt/lolcoach/game.mp4',
    '/var/tmp/game.mkv',
    '/workspace/data/league.db',
    r'C:\secret\video.mp4',
    '\\\\media\\share\\game.mkv',
    'https://private.example/match',
    'PUUID hidden',
    'riotIdGameName',
    'summonerId',
    'accountId',
    'raw_json',
    'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    'x' * 60,
    '{"metadata": {"participants": ["abc"]}, "info": {"gameVersion": "1"}}',
    'data:image/png;base64,private',
)


class ScanTests(unittest.TestCase):
    def test_examples_and_cloud_context_omit_identifiers(self):
        for path in EXAMPLES.glob('*.json'):
            privacy.assert_clean(path.read_text(encoding='utf-8'), path.name)
        _, cases = evaluation.load_dataset(EXAMPLES / 'eval_dataset.json')
        self.assertGreaterEqual(len(cases), 4)
        for case in cases:
            context = cloud.checked_text(case['packet'], case['observations'])
            text = json.dumps(context)
            privacy.assert_clean(text, case['spec']['id'])
            self.assertNotIn('image_file', text)
            self.assertNotIn('raw_json', text)
            for frame in case['packet']['frames']:
                self.assertNotIn(frame['image_file'], text)

    def test_checked_text_rejects_identifiers_paths_and_raw_blobs(self):
        packet = harness.read_json(EXAMPLES / 'packet.json')
        observations = harness.read_json(EXAMPLES / 'observations.json')
        for value in TOKENS:
            edited = dict(observations, unknowns=[value])
            with self.subTest(value=value[:48]), self.assertRaisesRegex(ValueError, 'Cloud text'):
                cloud.checked_text(packet, edited)
            self.assertTrue(privacy.findings(value))

    def test_ordinary_game_language_is_not_a_path_or_riot_id(self):
        packet = harness.read_json(EXAMPLES / 'packet.json')
        observations = harness.read_json(EXAMPLES / 'observations.json')
        observations = dict(observations, unknowns=['Wave is about 1/2 cleared. Ahri held mid.'])
        cloud.checked_text(packet, observations)
        self.assertEqual(privacy.findings('Wave is about 1/2 cleared. Ahri held mid.'), [])

    def test_local_review_request_refuses_a_path_before_return(self):
        _, cases = evaluation.load_dataset(EXAMPLES / 'eval_dataset.json')
        case = next(item for item in cases if item['spec']['id'] == 'visible-positioning')
        case['observations']['unknowns'].append('/opt/lolcoach/private.mp4')
        with self.assertRaisesRegex(ValueError, 'Cloud text'):
            evaluation.request_for(case, 'local', 'stub-local', 'review', 1024)


class ClipManifestTests(unittest.TestCase):
    def test_extra_name_path_and_raw_json_fields_are_dropped(self):
        dirty_match = dict(MATCH, puuid='my-private-puuid', summonerName='HiddenPlayer',
                           riotIdGameName='HiddenPlayer', riotIdTagline='NA9',
                           raw_json='{"puuid":"my-private-puuid","metadata":{"participants":["x"]}}',
                           recording_path='/home/me/Videos/game.mp4', win=0)
        dirty_event = dict(death(300000), puuid='private-puuid', summonerName='HiddenPlayer',
                           raw_json='{"metadata":{"participants":["private-puuid"]}}')
        manifest = clips.plan_clips(dirty_match, [dirty_event], -5, 600)
        text = json.dumps(manifest)
        privacy.assert_clean(text, 'Clip manifest')
        for token in ('puuid', 'HiddenPlayer', 'raw_json', 'metadata', '/home/', 'private-puuid', 'NA9'):
            self.assertNotIn(token, text)
        self.assertEqual(manifest['match']['my_champion'], 'Ahri')
        self.assertFalse(manifest['sync']['verified'])
        self.assertEqual(manifest['clips'][0]['events'][0]['victim'], 'Ahri')

    def test_a_riot_id_or_path_kept_in_a_public_field_is_refused(self):
        for patch in ('/home/me/secret',):
            with self.subTest(patch=patch), self.assertRaisesRegex(ValueError, 'Clip manifest'):
                clips.plan_clips(dict(MATCH, patch=patch), [death(300000)], 0, 600)
        with self.assertRaisesRegex(ValueError, 'Clip manifest'):
            clips.plan_clips(dict(MATCH, opp_champion='HiddenPlayer#NA9'), [death(300000)], 0, 600)

    def test_database_evidence_manifest_has_no_stored_identifiers(self):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        try:
            populate(conn)
            match, events = clips.match_evidence(conn, 'NA1_123')
            manifest = clips.plan_clips(match, events, 0, 600)
            text = json.dumps(manifest)
            privacy.assert_clean(text)
            for token in ('private-player', 'private-puuid', 'my-private-puuid', 'summonerName', 'raw_json'):
                self.assertNotIn(token, text)
            self.assertEqual(events[0]['victim'], 'Ahri')
        finally:
            conn.close()


class ExportPrivacyTests(unittest.TestCase):
    def test_scrubbed_demo_has_no_player_names_or_scan_hits(self):
        import tempfile
        from pathlib import Path as Fs

        from review_data import ReviewStore

        with tempfile.TemporaryDirectory() as temp:
            base = Fs(temp)
            db, notes = base / 'league.db', base / 'reviews.db'
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript(SCHEMA)
                mid = 'NA1_7000000001'
                store_match(conn, riot_match(mid, 1_000_000, True), ME)
                store_timeline(conn, mid, timeline())
            ReviewStore(db, notes)
            files = build(db, notes, count=1, riot_id='MySecretName#NA9')
            text = json.dumps(files)
            privacy.assert_clean(text, 'demo export')
            for secret in SECRETS:
                self.assertNotIn(secret, text)
            for word in ('puuid', 'riotId', 'summoner', 'raw_json'):
                self.assertNotIn(word, text)


class DatasetPrefixTests(unittest.TestCase):
    def test_dev_fixtures_cover_an_unverified_clock_and_a_control_hold(self):
        _, cases = evaluation.load_dataset(EXAMPLES / 'eval_dataset.json')
        by_id = {case['spec']['id']: case for case in cases}
        test_ids = [case['spec']['id'] for case in cases if case['spec']['split'] == 'test']
        self.assertEqual(test_ids, ['missing-footage', 'visible-positioning'])
        clock = by_id['clock-unverified']
        self.assertEqual(clock['spec']['split'], 'dev')
        self.assertEqual(clock['spec']['expected_assessment'], 'needs_more_evidence')
        self.assertFalse(clock['packet']['sync_verified'])
        self.assertIn('clock', clock['observations']['observations'][0]['statement'].lower())
        hold = by_id['held-wave']
        self.assertEqual(hold['spec']['expected_assessment'], 'reviewable')
        self.assertFalse(hold['packet']['sync_verified'])
        statement = hold['observations']['observations'][0]['statement'].lower()
        self.assertIn('ordinary decision', statement)
        self.assertTrue(hold['observations']['observations'][0]['id'].startswith('obs-'))

    def test_dataset_rejects_an_observation_id_without_the_obs_prefix(self):
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(EXAMPLES, root / 'examples')
            dataset = root / 'examples' / 'eval_dataset.json'
            observations = root / 'examples' / 'held_wave_observations.json'
            data = harness.read_json(observations)
            data['observations'][0]['id'] = 'note-hold'
            data['observations'][0]['evidence_refs'] = ['frame-190000']
            observations.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, 'obs-'):
                evaluation.load_dataset(dataset)


if __name__ == '__main__':
    unittest.main()
