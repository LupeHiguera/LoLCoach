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
SYNC_TOLERANCE_S = 1.0
SYNC_MIN_SPAN_S = 60.0
# A larger slope means the single offset is drifting; 0.2 s/min is 6 s over 30 min.
SYNC_MAX_DRIFT_S_PER_MIN = 0.2
SYNC_NOTE = ("video seconds = game seconds + offset_s. Offset is unverified; "
             "check the visible game clock before interpreting a clip. "
             "One offset assumes continuous footage with no cuts or pauses.")
SYNC_VERIFIED_NOTE = ("video seconds = game seconds + offset_s. "
                      "You accepted a clock check: at least two readings, 60 s apart, "
                      "each within 1 s, with the displayed second matching and drift "
                      "at most 0.2 s per minute. This does not prove sub-second accuracy "
                      "past the last reading, and it assumes no cuts or pauses.")
PUBLIC_MATCH = ('match_id', 'patch', 'my_champion', 'opp_champion', 'duration_s')
PUBLIC_EVENT = ('time', 'side', 'killer', 'victim', 'assists', 'me', 'x', 'y')
_FORBIDDEN_KEYS = {
    'puuid', 'summonername', 'summonerid', 'riotid', 'riotidgamename',
    'riotidtagline', 'gamename', 'tagline', 'rawjson',
}
_RIOT_ID = re.compile(r'\S+#\w+')
_LONG_TOKEN = re.compile(r'(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{60,}(?![A-Za-z0-9_-])')


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


def assert_no_player_identifiers(value):
    """Reject names, PUUIDs and raw JSON. Champion names and match ids stay.

    The error does not echo the offending text.
    """
    def walk(node):
        if isinstance(node, dict):
            for key, item in node.items():
                folded = re.sub(r'[^a-z0-9]', '', str(key).lower())
                if folded in _FORBIDDEN_KEYS:
                    raise ValueError('Manifest would store a player identifier field')
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and (_RIOT_ID.search(node) or _LONG_TOKEN.search(node)):
            raise ValueError('Manifest text looks like a Riot ID or PUUID')
    walk(value)


def public_match(match):
    """Copy only clip-planning fields. Win/loss, paths and raw JSON never enter."""
    if any(key not in match for key in ('match_id', 'duration_s')):
        raise ValueError('Match evidence is missing match_id or duration')
    return {key: match[key] for key in PUBLIC_MATCH if key in match}


def public_event(event):
    """Champion names and map coordinates only; extra participant fields are dropped."""
    if 'time' not in event:
        raise ValueError('Kill event is missing its timestamp')
    return {key: event[key] for key in PUBLIC_EVENT if key in event}


def parse_clock(text):
    """Seconds from a number or M:SS[.s]. The in-game clock shows whole seconds."""
    raw = str(text).strip()
    if ':' in raw:
        minutes, seconds = raw.split(':', 1)
        if not minutes.isdigit():
            raise ValueError(f'Clock must look like 1:01 or 61.5, not {raw!r}')
        try:
            second = float(seconds)
        except ValueError as exc:
            raise ValueError(f'Clock must look like 1:01 or 61.5, not {raw!r}') from exc
        if not math.isfinite(second) or second < 0 or second >= 60:
            raise ValueError(f'Clock seconds must be in 0..60, not {raw!r}')
        return int(minutes) * 60 + second
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f'Clock must look like 1:01 or 61.5, not {raw!r}') from exc
    if not math.isfinite(value) or value < 0:
        raise ValueError(f'Clock must be a nonnegative finite time, not {raw!r}')
    return value


def parse_checkpoint(text):
    """`video=clock`, both as seconds or M:SS. Example: 60=1:01."""
    video, separator, clock = str(text).partition('=')
    if not separator or not video.strip() or not clock.strip():
        raise ValueError('Checkpoint must look like 60=1:01')
    return parse_clock(video), parse_clock(clock)


def _slope(xs, ys):
    """Seconds of clock error per second of video; None when the readings share a time."""
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance


def measure_sync(offset_s, checkpoints, tolerance_s=SYNC_TOLERANCE_S):
    """Compare a saved offset with game clocks you read yourself.

    Mapping is `video_s = game_s + offset_s`, so the clock the offset predicts at a
    video time is `video_s - offset_s`. `error_s` is shown minus that prediction
    (negative means the visible clock is behind the offset).

    The in-game clock displays a whole second, so a fraction under 1 s is not a
    correction. `displayed_error_s` is `floor(shown) - floor(expected)`. A nonzero
    value, when every checkpoint agrees, is the whole-second correction:
    `suggested_offset_s = offset_s - displayed_error_s`. That is the README rule:
    expecting 5:00 and seeing 4:58 suggests `offset_s + 2`.

    `acceptable` needs two readings at least 60 s apart, every absolute error within
    `tolerance_s` (default 1 s), every displayed second matching, and drift at most
    0.2 s per minute. This does not watch the video and does not set `verified`.
    """
    if isinstance(offset_s, bool) or not isinstance(offset_s, (int, float)) or not math.isfinite(offset_s):
        raise ValueError('Recording offset must be finite')
    if isinstance(tolerance_s, bool) or not isinstance(tolerance_s, (int, float)) or not math.isfinite(tolerance_s) or tolerance_s <= 0:
        raise ValueError('Sync tolerance must be a positive number of seconds')
    if not checkpoints:
        raise ValueError('Read at least one game clock')
    rows = []
    seen = set()
    for video_s, shown_game_s in checkpoints:
        for label, value in (('Video time', video_s), ('Shown game clock', shown_game_s)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f'{label} must be a nonnegative finite number of seconds')
        if video_s in seen:
            raise ValueError('Two checkpoints use the same video time')
        seen.add(video_s)
        expected = video_s - offset_s
        error = shown_game_s - expected
        displayed = math.floor(shown_game_s) - math.floor(expected)
        rows.append(dict(video_s=round(video_s, 3), shown_game_s=round(shown_game_s, 3),
                         expected_game_s=round(expected, 3), error_s=round(error, 3),
                         displayed_error_s=displayed,
                         whole_second_match=displayed == 0))
    errors = [row['error_s'] for row in rows]
    displayed = [row['displayed_error_s'] for row in rows]
    ordered = sorted(rows, key=lambda row: row['video_s'])
    span = ordered[-1]['video_s'] - ordered[0]['video_s']
    drift = None
    if len(rows) >= 2 and span >= SYNC_MIN_SPAN_S:
        slope = _slope([row['video_s'] for row in ordered], [row['error_s'] for row in ordered])
        drift = None if slope is None else round(slope * 60, 3)
    same_display = len(set(displayed)) == 1
    suggested = round(offset_s - displayed[0], 3) if same_display else None
    within = all(abs(error) <= tolerance_s for error in errors)
    whole = all(row['whole_second_match'] for row in rows)
    drift_ok = drift is not None and abs(drift) <= SYNC_MAX_DRIFT_S_PER_MIN
    return dict(
        offset_s=offset_s, mapping='video_s = game_s + offset_s', tolerance_s=tolerance_s,
        checkpoints=rows, n=len(rows),
        median_error_s=round(statistics_median(errors), 3),
        max_abs_error_s=round(max(abs(error) for error in errors), 3),
        suggested_offset_s=suggested, drift_s_per_min=drift, span_s=round(span, 3),
        whole_seconds_agree=whole, within_tolerance=within,
        acceptable=len(rows) >= 2 and span >= SYNC_MIN_SPAN_S and within and whole and drift_ok,
        note=('Compared with clocks you read. Footage was not inspected. '
              'A fraction under one second is not a correction. '
              'One checkpoint cannot show drift. verified is not set here.'))


def statistics_median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def attach_sync_measurement(manifest, checkpoints, accept=False, tolerance_s=SYNC_TOLERANCE_S):
    """Record a clock check on a manifest. `verified` flips only when you accept a pass."""
    report = measure_sync(manifest['sync']['offset_s'], checkpoints, tolerance_s)
    manifest['sync']['measurement'] = report
    if accept:
        if not report['acceptable']:
            raise ValueError('Clock readings do not meet the sync check; verified stays false')
        manifest['sync']['verified'] = True
        manifest['sync']['note'] = SYNC_VERIFIED_NOTE
    assert_no_player_identifiers(manifest)
    return manifest


def plan_clips(match, events, offset_s, duration_s, before_s=BEFORE_S, after_s=AFTER_S):
    """Clamped source windows and evidence; missing footage is explicit, not inferred."""
    match = public_match(match)
    events = [public_event(event) for event in events]
    if not isinstance(offset_s, (int, float)) or isinstance(offset_s, bool) or not math.isfinite(offset_s):
        raise ValueError("Recording offset must be finite")
    positive(duration_s, 'Video duration')
    positive(before_s, 'Before window', True)
    positive(after_s, 'After window', True)
    if before_s + after_s == 0:
        raise ValueError("The review window must have a nonzero duration")
    clips, skipped = [], []
    for event in events:
        if event.get('me') != 'death':
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
    manifest = dict(schema_version=1, match=match,
                    sync=dict(offset_s=offset_s, verified=False, note=SYNC_NOTE),
                    requested_window=dict(before_s=before_s, after_s=after_s),
                    evidence_note='Kill events are Riot timeline facts, not causes or visibility at the time.',
                    clips=clips, skipped=skipped)
    assert_no_player_identifiers(manifest)
    return manifest


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
    assert_no_player_identifiers(manifest)
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
    parser.add_argument('--match', dest='match_id')
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
    parser.add_argument('--sync-only', action='store_true',
                        help='Compare --offset with --checkpoint readings; no database, video, or network')
    parser.add_argument('--checkpoint', action='append', default=[], metavar='VIDEO=CLOCK',
                        help='Game clock you read at a video time, e.g. 60=1:01. Repeat for drift')
    parser.add_argument('--accept-sync', action='store_true',
                        help='Mark the manifest verified only when the clock check passes')
    args = parser.parse_args(argv)
    if args.sync_only:
        if args.offset is None or not args.checkpoint:
            parser.error('--sync-only needs --offset and at least one --checkpoint')
        try:
            print(json.dumps(measure_sync(args.offset, [parse_checkpoint(item) for item in args.checkpoint]), indent=2))
        except ValueError as exc:
            parser.exit(1, f"Death clips: {exc}\n")
        return
    if not args.match_id:
        parser.error('--match is required unless --sync-only is set')
    if args.accept_sync and not args.checkpoint:
        parser.error('--accept-sync needs at least one --checkpoint')
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
        if args.checkpoint:
            attach_sync_measurement(manifest, [parse_checkpoint(item) for item in args.checkpoint],
                                    accept=args.accept_sync)
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
