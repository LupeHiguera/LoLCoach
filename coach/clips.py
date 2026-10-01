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

from review_data import connect, kill_feed

BEFORE_S = 60.0
AFTER_S = 10.0
SYNC_NOTE = ("video seconds = game seconds + offset_s. Offset is unverified; "
             "check the visible game clock before interpreting a clip. "
             "One offset assumes continuous footage with no cuts or pauses.")


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


def plan_clips(match, events, offset_s, duration_s, before_s=BEFORE_S, after_s=AFTER_S):
    """Clamped source windows and evidence; missing footage is explicit, not inferred."""
    if not isinstance(offset_s, (int, float)) or not math.isfinite(offset_s):
        raise ValueError("Recording offset must be finite")
    positive(duration_s, 'Video duration')
    positive(before_s, 'Before window', True)
    positive(after_s, 'After window', True)
    if before_s + after_s == 0:
        raise ValueError("The review window must have a nonzero duration")
    clips, skipped = [], []
    for event in events:
        if event['me'] != 'death':
            continue
        ts = event['time']
        source_death = ts / 1000 + offset_s
        moment_id = f'death-{ts}'
        if not 0 <= source_death < duration_s:
            skipped.append(dict(moment_id=moment_id, death_game_ms=ts,
                                reason='Death timestamp falls outside the recording'))
            continue
        game_start = max(0, ts / 1000 - before_s)
        game_end = min(match['duration_s'], ts / 1000 + after_s)
        start = max(0, game_start + offset_s)
        end = min(duration_s, game_end + offset_s)
        if end <= start:
            skipped.append(dict(moment_id=moment_id, death_game_ms=ts,
                                reason='No footage in the requested review window'))
            continue
        actual_start_ms = (start - offset_s) * 1000
        actual_end_ms = (end - offset_s) * 1000
        clips.append(dict(
            moment_id=moment_id, file=f'{moment_id}.mp4', death_game_ms=ts,
            source_start_s=start, source_end_s=end,
            game_start_ms=actual_start_ms, game_end_ms=actual_end_ms,
            death_clip_s=source_death - start,
            truncated_before=start > game_start + offset_s,
            truncated_after=end < game_end + offset_s,
            events=[dict(e, clip_s=(e['time'] - actual_start_ms) / 1000)
                    for e in events if actual_start_ms <= e['time'] <= actual_end_ms]))
    return dict(schema_version=1, match=match,
                sync=dict(offset_s=offset_s, verified=False, note=SYNC_NOTE),
                requested_window=dict(before_s=before_s, after_s=after_s),
                evidence_note='Kill events are Riot timeline facts, not causes or visibility at the time.',
                clips=clips, skipped=skipped)


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
