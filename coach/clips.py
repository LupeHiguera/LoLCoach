"""Extract death review clips locally: python -m coach.clips --help.

Timeline events locate moments; they do not establish why a death happened.
No models, network calls, or changes to either source database are made.
"""
import argparse
import json
import math
import re
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from coach.sync import SYNC_NOTE, game_seconds, live_clock_sync, offset_sync, video_seconds
from review_data import connect, kill_feed

BEFORE_S = 60.0
AFTER_S = 10.0
# A death skipped for this reason was not placed. It is not a truncated clip.
LOADING_SKIP_REASON = (
    "Live Client gameTime was not positive (loading screen, not HUD 0:00); "
    "death was not placed on the video")


def match_evidence(conn, match_id):
    """Whitelisted match facts and kill events, never names, PUUIDs, or raw JSON."""
    row = conn.execute("""SELECT match_id, patch, my_champion, opp_champion,
        duration_s, my_participant_id FROM matches WHERE match_id=?""",
        (match_id,)).fetchone()
    if row is None:
        raise ValueError("Match not found in the match database")
    if conn.execute("SELECT 1 FROM timelines WHERE match_id=?", (match_id,)).fetchone() is None:
        raise ValueError("This match has no timeline; fetch it before extracting death clips")
    match = dict(row)
    me = match.pop('my_participant_id')
    return match, kill_feed(conn, match_id, me)


def linked_video(notes_path, match_id):
    """Only finished, linked footage is eligible; no schema or link writes."""
    with closing(connect(notes_path, True)) as conn:
        row = conn.execute("""SELECT path, offset_s FROM recordings
            WHERE match_id=? AND status='linked' AND path IS NOT NULL
            ORDER BY id DESC LIMIT 1""", (match_id,)).fetchone()
    if row is None:
        raise ValueError("No finished recording linked. Run recorder.py --link "
                         "after fetching, or supply --video and --offset")
    return Path(row['path']).resolve(), row['offset_s']


def positive(value, label, allow_zero=False):
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{label} must be finite and {'nonnegative' if allow_zero else 'positive'}")


def _windows(duration_s, before_s, after_s):
    positive(duration_s, 'Video duration')
    positive(before_s, 'Before window', True)
    positive(after_s, 'After window', True)
    if before_s + after_s == 0:
        raise ValueError("The review window must have a nonzero duration")


def _sync_for_plan(offset_s, sync):
    """Attach a sync block that cannot claim verified sync.

    A supplied block must already say verified false and carry the same
    offset. A loading-clock block has no mapping and cannot place deaths.
    """
    if sync is None:
        block = offset_sync(offset_s)
        block['note'] = SYNC_NOTE
        return block
    if not isinstance(sync, dict) or sync.get('verified') is not False:
        raise ValueError('Sync cannot be marked verified')
    if sync.get('anchor') == 'loading_clock' or sync.get('offset_s') is None:
        raise ValueError('Loading-clock sync has no video mapping; deaths cannot be placed')
    if sync.get('offset_s') != offset_s:
        raise ValueError('Sync offset does not match the planning offset')
    return dict(sync, verified=False)


def review_window(death_game_s, offset_s, duration_s, match_duration_s, before_s, after_s):
    """Place one death on a file, or say why it was not placed.

    kind 'clip' is a cut. truncated_before / truncated_after mean the
    requested window was shortened by video 0, the end of the file, or
    game 0 / the end of the match. They do not mean the HUD was read.
    kind 'skip' means the death was not placed. A skip is not a truncation:
    there is no partial clip. sync_verified is false in both cases.
    """
    death_video_s = video_seconds(death_game_s, offset_s)
    if not 0 <= death_video_s < duration_s:
        return dict(kind='skip', reason='Death timestamp falls outside the recording',
                    sync_verified=False)
    game_start_s = max(0, death_game_s - before_s)
    game_end_s = min(match_duration_s, death_game_s + after_s)
    raw_start = video_seconds(game_start_s, offset_s)
    raw_end = video_seconds(game_end_s, offset_s)
    start = max(0, raw_start)
    end = min(duration_s, raw_end)
    if end <= start:
        return dict(kind='skip', reason='No footage in the requested review window',
                    sync_verified=False)
    return dict(kind='clip', start_s=start, end_s=end,
                game_start_s=game_seconds(start, offset_s),
                game_end_s=game_seconds(end, offset_s),
                death_video_s=death_video_s,
                truncated_before=start > raw_start,
                truncated_after=end < raw_end,
                sync_verified=False)


def _manifest(match, before_s, after_s, sync, clips, skipped):
    return dict(schema_version=1, match=match, sync=sync,
                requested_window=dict(before_s=before_s, after_s=after_s),
                evidence_note='Kill events are Riot timeline facts, not causes or visibility at the time.',
                clips=clips, skipped=skipped)


def _loading_manifest(match, events, duration_s, before_s, after_s, sync):
    """Deaths stay listed and unplaced. Truncation flags are not invented."""
    _windows(duration_s, before_s, after_s)
    skipped = [dict(moment_id=f"death-{event['time']}", death_game_ms=event['time'],
                    reason=LOADING_SKIP_REASON, sync_verified=False)
               for event in events if event['me'] == 'death']
    return _manifest(match, before_s, after_s, sync, [], skipped)


def plan_clips(match, events, offset_s, duration_s, before_s=BEFORE_S, after_s=AFTER_S, *, sync=None):
    """Clamped source windows and evidence; missing footage is explicit, not inferred.

    offset_s is video_seconds - game_seconds for one continuous file.
    sync.verified is false. Pass a checkpoint or live-clock block to keep
    its note; a block with verified true is rejected.
    """
    sync = _sync_for_plan(offset_s, sync)
    _windows(duration_s, before_s, after_s)
    clips, skipped = [], []
    for event in events:
        if event['me'] != 'death':
            continue
        ts = event['time']
        moment_id = f'death-{ts}'
        placed = review_window(ts / 1000, offset_s, duration_s, match['duration_s'], before_s, after_s)
        if placed['kind'] == 'skip':
            skipped.append(dict(moment_id=moment_id, death_game_ms=ts,
                                reason=placed['reason'], sync_verified=False))
            continue
        start, end = placed['start_s'], placed['end_s']
        actual_start_ms = placed['game_start_s'] * 1000
        actual_end_ms = placed['game_end_s'] * 1000
        clips.append(dict(
            moment_id=moment_id, file=f'{moment_id}.mp4', death_game_ms=ts,
            source_start_s=start, source_end_s=end,
            game_start_ms=actual_start_ms, game_end_ms=actual_end_ms,
            death_clip_s=placed['death_video_s'] - start,
            truncated_before=placed['truncated_before'],
            truncated_after=placed['truncated_after'],
            sync_verified=False,
            events=[dict(e, clip_s=(e['time'] - actual_start_ms) / 1000)
                    for e in events if actual_start_ms <= e['time'] <= actual_end_ms]))
    return _manifest(match, before_s, after_s, sync, clips, skipped)


def plan_clips_from_live_clock(match, events, game_time_s, duration_s,
                               before_s=BEFORE_S, after_s=AFTER_S):
    """Place deaths from a Live Client reading taken when the file began.

    A positive reading uses offset = -game_time_s and still marks sync
    unverified. A non-positive reading is the loading screen: every death
    is skipped, offset_s is None, and nothing is labelled truncated.
    The 74s loading miss is described on the sync note and is not applied.
    """
    sync = live_clock_sync(game_time_s)
    if sync['offset_s'] is None:
        return _loading_manifest(match, events, duration_s, before_s, after_s, sync)
    return plan_clips(match, events, sync['offset_s'], duration_s, before_s, after_s, sync=sync)


def run_media(command):
    """Run local executables without a shell; surface a concise failure."""
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True).stdout
    except FileNotFoundError as exc:
        raise ValueError(f"{command[0]} not found. Install FFmpeg with ffprobe or pass its path") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"{command[0]} failed: {exc.stderr[-1000:]}") from exc


def video_duration(video, ffprobe='ffprobe'):
    """Probe the video stream, rather than a potentially longer audio track."""
    result = json.loads(run_media([str(ffprobe), '-v', 'error', '-select_streams', 'v:0',
                                  '-show_entries', 'stream=duration:format=duration',
                                  '-of', 'json', str(video)]))
    streams = result.get('streams', [])
    if not streams:
        raise ValueError("Recording has no video stream")
    value = streams[0].get('duration')
    if value in (None, 'N/A'):
        value = result.get('format', {}).get('duration')
    try:
        duration = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Could not determine recording duration") from exc
    positive(duration, 'Video duration')
    return duration


def extract_clips(manifest, video, output, ffmpeg='ffmpeg'):
    """Publish a complete batch atomically; existing batches are never overwritten."""
    match_id = manifest['match']['match_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', match_id):
        raise ValueError("Match ID is not a safe output directory name")
    output = Path(output)
    destination = output / match_id
    if destination.exists():
        raise ValueError(f"Output already exists: {destination}. Choose another --output directory")
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.clips-', dir=output) as temp:
        staging = Path(temp)
        for clip in manifest['clips']:
            run_media([str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin', '-n',
                       '-ss', str(clip['source_start_s']), '-i', str(video),
                       '-t', str(clip['source_end_s'] - clip['source_start_s']),
                       '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264',
                       '-preset', 'fast', '-crf', '18', '-pix_fmt', 'yuv420p',
                       '-c:a', 'aac', '-movflags', '+faststart',
                       str(staging / clip['file'])])
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
        # rename fails if another complete batch appeared while extraction ran.
        staging.rename(destination)
    return destination


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--match', required=True, dest='match_id')
    parser.add_argument('--db', default='league.db')
    parser.add_argument('--notes', default='reviews.db')
    parser.add_argument('--video', type=Path, help='Override linked recording with a completed local file')
    parser.add_argument('--offset', type=float, help='Video seconds at game 0:00; may be negative')
    parser.add_argument('--before', type=float, default=BEFORE_S)
    parser.add_argument('--after', type=float, default=AFTER_S)
    parser.add_argument('--output', type=Path, default=Path('data/clips'))
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--ffprobe', default='ffprobe')
    parser.add_argument('--dry-run', action='store_true', help='Print the manifest; do not extract or write files')
    args = parser.parse_args(argv)
    if (args.video is None) != (args.offset is None):
        parser.error('Use --video and --offset together')
    try:
        with closing(connect(args.db, True)) as conn:
            match, events = match_evidence(conn, args.match_id)
        if args.video is None:
            video, offset = linked_video(args.notes, args.match_id)
            sync_source = 'recorder_clock'
            if offset is None:
                raise ValueError(
                    "Linked recording has no offset. The live clock was not a positive "
                    "game time, so video 0 was not labelled as HUD 0:00. "
                    "Check the visible clock and pass --video with --offset")
        else:
            video, offset = args.video.resolve(), args.offset
            sync_source = 'manual'
        if not video.is_file():
            raise ValueError(f"Recording file not found: {video}")
        manifest = plan_clips(match, events, offset, video_duration(video, args.ffprobe),
                              args.before, args.after)
        manifest['sync']['source'] = sync_source
        if args.dry_run:
            print(json.dumps(manifest, indent=2))
        else:
            destination = extract_clips(manifest, video, args.output, args.ffmpeg)
            print(f"Saved {len(manifest['clips'])} death clips to {destination}; "
                  f"{len(manifest['skipped'])} deaths outside available footage")
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"Death clips: {exc}\n")


if __name__ == '__main__':
    main()
