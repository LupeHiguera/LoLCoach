"""Local observer views without changing the shared moment packet.

Prepare eight earlier timestamps with coach.harness prepare --frames 8. Then use
prepare here to add native-resolution crops from that clip, and observe to send
selected views to a loopback model. Crop rectangles are explicit source pixels, x:y:w:h.
Reviewers continue to consume the original packet and text observations only.
"""
import argparse
import base64
import hashlib
import json
import shutil
import struct
import tempfile
from pathlib import Path

from coach import harness
from coach.clips import run_media
from coach.contracts import MAX_FRAMES, MAX_TEXT_CHARS, validate_observations, validate_packet
from recorder import live_game_time

REGIONS = ('hud', 'minimap')
MAX_VIEWS = MAX_FRAMES * 3
MAX_TOTAL_IMAGE_BYTES = 16 << 20
VIEW_PROMPT = """Each frame ID has a whole-frame image. Some frame IDs also have
native-resolution HUD and minimap crops of the SAME instant. Crops are spatial views, not later times.
Cite the parent frame ID and name the view used in the statement. Camera movement
does not prove champion movement. Read only legible numbers; do not infer ability
names, casts or readiness from colours alone. Do not transcribe player names or chat.
Separate visible facts from unknowns; abstain when champion identity is unclear."""


def fingerprint(packet):
    return hashlib.sha256(json.dumps(packet, sort_keys=True).encode()).hexdigest()


def rectangle(value):
    try:
        result = [int(v) for v in value.split(':')]
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Crop must be x:y:w:h in source pixels') from exc
    if len(result) != 4 or min(result[:2]) < 0 or min(result[2:]) <= 0:
        raise argparse.ArgumentTypeError('Crop requires nonnegative x/y and positive width/height')
    return result


def validate_views(views, packet):
    """Crops cannot add a new timestamp or detach from their source packet."""
    validate_packet(packet)
    if (set(views) != {'schema_version', 'moment_id', 'packet_sha256', 'source_size', 'crops'} or
            views['schema_version'] != 1 or views['moment_id'] != packet['moment_id'] or
            views['packet_sha256'] != fingerprint(packet)):
        raise ValueError('Views do not match the moment packet')
    size = views['source_size']
    if not isinstance(size, list) or len(size) != 2 or any(type(v) is not int or v <= 0 for v in size):
        raise ValueError('Invalid source dimensions')
    frames = {f['id']: f for f in packet['frames']}
    crops = views['crops']
    if not frames or not isinstance(crops, list) or len(crops) != len(frames) * len(REGIONS):
        raise ValueError('Each timestamp requires exactly one HUD and one minimap crop')
    seen, files = set(), {f['image_file'] for f in frames.values()}
    for crop in crops:
        if set(crop) != {'frame_id', 'game_ms', 'region', 'image_file', 'rect'}:
            raise ValueError('Unexpected crop fields')
        frame = frames.get(crop['frame_id'])
        if frame is None or type(crop['game_ms']) is not int or crop['game_ms'] != frame['game_ms']:
            raise ValueError('Crop timestamp must match its parent frame')
        pair = (crop['frame_id'], crop['region'])
        if crop['region'] not in REGIONS or pair in seen:
            raise ValueError('Unknown or duplicate crop region')
        seen.add(pair)
        name = crop['image_file']
        if name != f"{crop['region']}-{crop['game_ms']}.png" or name in files:
            raise ValueError('Invalid or duplicate crop filename')
        files.add(name)
        rect = crop['rect']
        if not isinstance(rect, list) or len(rect) != 4 or any(type(v) is not int for v in rect):
            raise ValueError('Invalid crop rectangle')
        x, y, w, h = rect
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > size[0] or y + h > size[1]:
            raise ValueError('Crop extends outside source video')
        if w * h > harness.MAX_IMAGE_PIXELS:
            raise ValueError('Native crop exceeds the pixel budget; choose a smaller rectangle')
    if len(frames) + len(crops) > MAX_VIEWS:
        raise ValueError('Observer image count exceeds budget')


def checked_image(root, view, native=False):
    url = harness.image_data(root, view)
    data = base64.b64decode(url.split(',', 1)[1])
    if native and tuple(view['rect'][2:]) != struct.unpack('>II', data[16:24]):
        raise ValueError('Crop dimensions differ from the native source rectangle')
    return url, len(data)


def prepare(manifest_path, packet_path, output, rectangles, ffmpeg='ffmpeg', ffprobe='ffprobe'):
    """Publish a separate complete bundle; leave the source packet and clip untouched."""
    manifest, packet = harness.read_json(manifest_path), harness.read_json(packet_path)
    validate_packet(packet)
    candidates = []
    for clip in manifest['clips']:
        start, end = packet['start_ms'], packet['decision_ms']
        opaque = hashlib.sha256(f"{manifest['match']['match_id']}:{clip['moment_id']}:{start}:{end}".encode()).hexdigest()[:16]
        if packet['moment_id'] == f'moment-{opaque}':
            candidates.append(clip)
    if len(candidates) != 1:
        raise ValueError('Packet does not belong to exactly one clip in this manifest')
    clip = candidates[0]
    if not clip['game_start_ms'] <= packet['start_ms'] <= packet['decision_ms'] < min(clip['game_end_ms'], clip['death_game_ms']):
        raise ValueError('Packet window must precede the death within the clip')
    video = harness.local_file(Path(manifest_path).parent, clip['file'])
    info = json.loads(run_media([str(ffprobe), '-v', 'error', '-select_streams', 'v:0',
                                '-show_entries', 'stream=width,height', '-of', 'json', str(video)]))
    stream = info['streams'][0]
    views = dict(schema_version=1, moment_id=packet['moment_id'], packet_sha256=fingerprint(packet),
                 source_size=[stream['width'], stream['height']], crops=[])
    for frame in packet['frames']:
        for region in REGIONS:
            views['crops'].append(dict(frame_id=frame['id'], game_ms=frame['game_ms'], region=region,
                                      image_file=f"{region}-{frame['game_ms']}.png", rect=rectangles[region]))
    validate_views(views, packet)
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError('Output already exists; choose a new bundle')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.views-', dir=output.parent) as temporary:
        staging = Path(temporary)
        total = 0
        for frame in packet['frames']:
            _, count = checked_image(Path(packet_path).parent, frame)
            total += count
            shutil.copyfile(harness.local_file(Path(packet_path).parent, frame['image_file']), staging / frame['image_file'])
        for crop in views['crops']:
            x, y, w, h = crop['rect']
            position = (crop['game_ms'] - clip['game_start_ms']) / 1000
            run_media([str(ffmpeg), '-v', 'error', '-nostdin', '-n', '-ss', str(position), '-i', str(video),
                       '-frames:v', '1', '-vf', f'crop={w}:{h}:{x}:{y}:exact=1',
                       '-compression_level', '9', str(staging / crop['image_file'])])
            _, count = checked_image(staging, crop, native=True)
            total += count
            if total > MAX_TOTAL_IMAGE_BYTES:
                raise ValueError('Observer images exceed the total byte budget')
        harness.write_output(packet, staging / 'packet.json')
        harness.write_output(views, staging / 'views.json')
        staging.rename(output)
    return output


def build_request(packet, views, root, model, preview=False, max_tokens=8192, reasoning_effort='low', crop_frames='last'):
    """Keep every whole frame; send decision crops by default, or explicitly all crops."""
    validate_views(views, packet)
    if crop_frames not in ('last', 'all'):
        raise ValueError('Crop frames must be last or all')
    payload = harness.build_request('observe', packet, model, preview=True,
                                    max_tokens=max_tokens, reasoning_effort=reasoning_effort)
    payload['messages'][0]['content'] += '\n' + VIEW_PROMPT
    content = payload['messages'][1]['content'][:1]
    total = 0
    last_time = max(f['game_ms'] for f in packet['frames'])
    for frame in packet['frames']:
        selected = [(dict(frame, region='whole'), False)]
        if crop_frames == 'all' or frame['game_ms'] == last_time:
            selected += [(c, True) for c in views['crops'] if c['frame_id'] == frame['id']]
        for view, native in selected:
            label = f"Frame {frame['id']} at game {frame['game_ms']} ms; {view['region']} view"
            if native:
                label += f"; source x,y,w,h={view['rect']} in {views['source_size']} pixels, no resizing"
            if preview:
                url = '<local image omitted>'
            else:
                url, count = checked_image(root, view, native)
                total += count
                if total > MAX_TOTAL_IMAGE_BYTES:
                    raise ValueError('Observer images exceed the total byte budget')
            content.extend([dict(type='text', text=label), dict(type='image_url', image_url=dict(url=url))])
    payload['messages'][1]['content'] = content
    text_size = len(payload['messages'][0]['content']) + len(json.dumps(payload['response_format']))
    text_size += sum(len(c['text']) for c in content if c['type'] == 'text')
    if text_size > MAX_TEXT_CHARS:
        raise ValueError('Observer text exceeds the request budget')
    return payload


def check_sheet(packet, views, observations):
    """Pending human decisions only; generating a sheet does not verify any claim."""
    validate_views(views, packet)
    validate_observations(observations, packet)
    frames = {f['id']: f for f in packet['frames']}
    return dict(schema_version=1, moment_id=packet['moment_id'], packet_sha256=fingerprint(packet),
                observations_sha256=fingerprint(observations),
                instructions='Compare each claim with the clip and cited views. Set verdict to correct, incorrect or unreadable; explain corrections. Pending is not verified.',
                checks=[dict(observation_id=o['id'], statement=o['statement'], game_ms=o['game_ms'],
                             evidence=[dict(frame_id=ref, whole=frames[ref]['image_file'],
                                            crops=[c['image_file'] for c in views['crops'] if c['frame_id'] == ref])
                                       for ref in o['evidence_refs']], verdict='pending', correction='')
                        for o in observations['observations']])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare', help='Add native crops to an existing earlier eight-frame packet')
    prep.add_argument('--manifest', type=Path, required=True)
    prep.add_argument('--packet', type=Path, required=True)
    prep.add_argument('--output', type=Path, required=True)
    for region in REGIONS:
        prep.add_argument('--' + region, required=True, type=rectangle, metavar='X:Y:W:H')
    prep.add_argument('--ffmpeg', default='ffmpeg')
    prep.add_argument('--ffprobe', default='ffprobe')
    observe = sub.add_parser('observe')
    observe.add_argument('--bundle', type=Path, required=True)
    observe.add_argument('--model', required=True)
    observe.add_argument('--base-url', default=harness.DEFAULT_BASE_URL)
    observe.add_argument('--output', type=Path, required=True)
    observe.add_argument('--max-tokens', type=int, default=8192)
    observe.add_argument('--timeout-s', type=float, default=300)
    observe.add_argument('--reasoning-effort', choices=harness.REASONING_EFFORTS, default='low')
    observe.add_argument('--crop-frames', choices=('last', 'all'), default='last',
                         help='Send crops at the final sampled instant (default), or every instant')
    observe.add_argument('--dry-run', action='store_true')
    check = sub.add_parser('check-sheet')
    check.add_argument('--bundle', type=Path, required=True)
    check.add_argument('--observations', type=Path, required=True)
    check.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            prepare(args.manifest, args.packet, args.output, {r: getattr(args, r) for r in REGIONS}, args.ffmpeg, args.ffprobe)
            print(f'Prepared local observer views in {args.output}')
            return
        packet = harness.read_json(args.bundle / 'packet.json')
        views = harness.read_json(args.bundle / 'views.json')
        if args.command == 'check-sheet':
            harness.write_output(check_sheet(packet, views, harness.read_json(args.observations)), args.output)
            return
        harness.local_endpoint(args.base_url)
        if args.output.exists() or args.output.is_symlink():
            raise ValueError('Output already exists; choose another path')
        if not args.dry_run and live_game_time() is not None:
            raise ValueError('Post-game only: wait until the game ends')
        payload = build_request(packet, views, args.bundle, args.model, args.dry_run, args.max_tokens, args.reasoning_effort, args.crop_frames)
        if args.dry_run:
            harness.write_output(payload)
        else:
            result = harness.complete(args.base_url, payload, timeout_s=args.timeout_s)
            validate_observations(result, packet, model_output=True)
            harness.write_output(result, args.output)
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        parser.exit(1, f'Observer views: {exc}\n')


if __name__ == '__main__':
    main()
