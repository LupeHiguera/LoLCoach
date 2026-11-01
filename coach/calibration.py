"""Correct a recording offset from human game-clock checkpoints.

The mapping is video seconds = game seconds + offset_s. A checkpoint is the
game time the current offset assigns to a paused frame (expected) and the
clock visible on that frame (shown). The corrected offset is the current
offset plus the mean of (expected minus shown). Expecting 5:00 and seeing
4:58 adds 2 seconds.

The report gives that mean, the spread of the corrections (max minus min,
not a confidence interval), and whether the clocks were read to the whole
second or to a written fraction. Spread wider than the coarsest reading
grain means those frames do not share one offset. One checkpoint has no
second reading to compare, so its spread of 0 is not agreement.

Checkpoint order is not a timeline. The same correction at two frames does
not measure drift between them or after the last one, and cuts, pauses and
speed changes are not detected. verified stays false. human_confirmed stays
false until the caller sets it after a person has read the frames; confirming
the readings still leaves drift unchecked, so the recording sync stays
unverified.

A loading-screen miss is only a checkpoint someone passes in. On one ranked
recording, offset 0 had been stored from a loading-screen clock, and video
10:00 showed HUD 8:46. That pair corrects by +74 seconds. Loading length
varies. 74 is not added unless those two times are the inputs.

python -m coach.calibration --offset=-95.5 --checkpoint 5:00 4:58
Exit 0 when the spread is within the reading grain, including a single
checkpoint. Exit 3 when the checkpoints disagree; the report is still
printed. Exit 1 when the input is rejected.
"""
import argparse
import importlib.util
import json
import math
import re

# A whole-second HUD digit covers one second. Spread above the coarsest
# grain of the readings is more than that digit can explain. The grain is
# not an error bar for the rest of the match.
WHOLE_SECOND_S = 1.0
DISAGREEMENT_EXIT = 3
_OUTPUT_DIGITS = 9
# A spread that rounds onto the grain is not wider than the grain.
_GRAIN_EPS_S = 1e-9

_PRECISION_TEXT = {
    'whole_second': 'whole-second',
    'subsecond': 'subsecond',
    'mixed': 'mixed whole-second and subsecond',
}


def _clean(value):
    """Round to the nearest nanosecond so binary dust is not a reading."""
    cleaned = round(float(value), _OUTPUT_DIGITS)
    if cleaned == 0:
        return 0.0
    return cleaned


def _fmt(value):
    text = f'{_clean(value):.{_OUTPUT_DIGITS}f}'.rstrip('0').rstrip('.')
    if text in ('', '-0'):
        return '0'
    return text


def _require_seconds(value, label):
    """Finite int or float seconds. True is a boolean, not 1 second."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number of seconds')


def video_seconds(game_s, offset_s):
    """Video position for a game-clock label: game_s + offset_s.

    The sum is the mapping, not evidence the frame shows game_s.
    """
    _require_seconds(game_s, 'Game time')
    _require_seconds(offset_s, 'Offset')
    return game_s + offset_s


def game_seconds(video_s, offset_s):
    """Game-clock label for a video position: video_s - offset_s.

    Inverse of the video-seconds mapping. The result is the label, not the HUD.
    """
    _require_seconds(video_s, 'Video time')
    _require_seconds(offset_s, 'Offset')
    return video_s - offset_s


# Kept if coach.sync replaces the public names. Same two formulas, no other sync behaviour.
_video_seconds_local = video_seconds
_game_seconds_local = game_seconds


def _bind_conversion():
    """Use coach.sync's mapping when that module is present.

    Both are video = game + offset and game = video - offset. Nothing else
    is borrowed: this module does not store offsets or decide that a file is synced.
    """
    global video_seconds, game_seconds
    if importlib.util.find_spec('coach.sync') is None:
        return
    from coach.sync import game_seconds as sync_game_seconds, video_seconds as sync_video_seconds
    video_seconds = sync_video_seconds
    game_seconds = sync_game_seconds


_bind_conversion()


def expected_game_seconds(video_s, offset_s):
    """Game time the current offset assigns at video_s.

    This is the expected clock for a checkpoint taken at that frame. It is
    the label from the offset, not the HUD, and it does not correct anything.
    """
    return game_seconds(video_s, offset_s)


def _float_resolution(value):
    """Grain of a bare float. An integer value did not report a fraction."""
    if math.isfinite(value) and (type(value) is int or value.is_integer()):
        return WHOLE_SECOND_S, 'whole_second'
    token = f'{value:.6f}'.rstrip('0').rstrip('.')
    if '.' not in token:
        return 1e-6, 'subsecond'
    return 10 ** (-len(token.split('.', 1)[1])), 'subsecond'


def _parse_number(text, label, allow_negative):
    if re.fullmatch(r'[+-]?\d+(?:\.\d+)?', text) is None:
        raise ValueError(f'{label} must be seconds or a clock like M:SS')
    if text.startswith('-') and not allow_negative:
        raise ValueError(f'{label} cannot be negative')
    sign = -1 if text.startswith('-') else 1
    body = text[1:] if text[0] in '+-' else text
    whole, _, frac = body.partition('.')
    seconds = sign * (int(whole) + (int(frac) / 10 ** len(frac) if frac else 0))
    if not frac:
        return float(sign * int(whole)), 'whole_second', WHOLE_SECOND_S
    return seconds, 'subsecond', 10 ** (-len(frac))


def _parse_clock(text, label):
    if text.startswith('-'):
        raise ValueError(f'{label} clock cannot be negative')
    match = re.fullmatch(r'(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)', text)
    if match is None:
        raise ValueError(f'{label} must be seconds or a clock like M:SS')
    hours_text, minutes_text, seconds_text = match.groups()
    minutes = int(minutes_text)
    if '.' in seconds_text:
        whole, frac = seconds_text.split('.', 1)
        seconds = int(whole) + int(frac) / 10 ** len(frac)
        resolution, precision = 10 ** (-len(frac)), 'subsecond'
    else:
        seconds = int(seconds_text)
        resolution, precision = WHOLE_SECOND_S, 'whole_second'
    if seconds >= 60:
        raise ValueError(f'{label} seconds must be under 60')
    if hours_text is None:
        total = minutes * 60 + seconds
    else:
        if minutes >= 60:
            raise ValueError(f'{label} minutes must be under 60 when hours are written')
        total = int(hours_text) * 3600 + minutes * 60 + seconds
    return total, precision, resolution


def parse_game_time(value, label, allow_negative=False):
    """One clock reading: seconds, whole_second or subsecond, and the grain.

    A clock string or numeric string that writes a fraction, including .0,
    is subsecond at that written grain (4:58.2 is a tenth, 4:58.0 is still
    a tenth). An int, or a float with no fractional part, is a whole second:
    the fraction was not reported. Negative numbers are labels from an
    offset, allowed only when allow_negative is set. A negative clock string
    is rejected. This parses a reading; it does not check a frame.
    """
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a game time in seconds or M:SS, not a boolean')
    if type(value) is int:
        seconds, precision, resolution = float(value), 'whole_second', WHOLE_SECOND_S
    elif type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f'{label} must be a finite number of seconds')
        resolution, precision = _float_resolution(value)
        seconds = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f'{label} is empty')
        if ':' in text:
            seconds, precision, resolution = _parse_clock(text, label)
        else:
            seconds, precision, resolution = _parse_number(text, label, allow_negative)
    else:
        raise ValueError(f'{label} must be a game time in seconds or M:SS')
    seconds = _clean(seconds)
    if seconds < 0 and not allow_negative:
        raise ValueError(f'{label} cannot be negative')
    return dict(seconds=seconds, precision=precision, resolution_s=resolution)


def _checkpoint_pair(item):
    if isinstance(item, dict):
        if set(item) != {'expected_game_time', 'shown_game_time'}:
            raise ValueError('A checkpoint dict needs expected_game_time and shown_game_time only')
        return item['expected_game_time'], item['shown_game_time']
    if isinstance(item, (list, tuple)) and len(item) == 2:
        return item[0], item[1]
    raise ValueError('A checkpoint is (expected_game_time, shown_game_time)')


def _require_offset(offset_s):
    if type(offset_s) not in (int, float) or not math.isfinite(offset_s):
        raise ValueError('Offset must be a finite number of seconds')
    return _clean(offset_s)


def _combined_precision(kinds):
    if kinds == {'whole_second'}:
        return 'whole_second'
    if kinds == {'subsecond'}:
        return 'subsecond'
    return 'mixed'


def _note(offset_s, current_offset_s, correction_s, spread_s, n_checkpoints,
          precision, resolution_s, agreement, human_confirmed):
    grain = _fmt(resolution_s)
    parts = [
        f'Corrected offset is {_fmt(offset_s)}s (current {_fmt(current_offset_s)}s '
        f'plus mean correction {_fmt(correction_s)}s).',
        f'{n_checkpoints} checkpoint{"" if n_checkpoints == 1 else "s"}, '
        f'{_PRECISION_TEXT[precision]} precision, grain {grain}s.',
    ]
    if n_checkpoints == 1:
        parts.append('Spread is 0 because one reading has nothing to disagree with. '
                     'That 0 is not a confidence interval.')
    elif agreement == 'disagreement':
        parts.append(f'Spread is {_fmt(spread_s)}s, wider than the {grain}s grain, '
                     'so one offset does not fit every checkpoint. '
                     'Spread is the range of the corrections, not a confidence interval.')
    elif spread_s == 0:
        parts.append('Spread is 0: the reported corrections match. '
                     'Spread is the range of the corrections, not a confidence interval. '
                     'Matching numbers are not a measurement of the rest of the file.')
    else:
        parts.append(f'Spread is {_fmt(spread_s)}s, within the {grain}s grain. '
                     'The corrections are not the same number. '
                     'Spread is the range of the corrections, not a confidence interval.')
    parts.append('Checkpoint order is not a timeline. Drift along the match is not measured. '
                 'Cuts, pauses and speed changes are not detected.')
    if precision != 'subsecond':
        parts.append('A whole-second reading does not confirm the fractional part of the stored offset.')
    if human_confirmed:
        parts.append('A person confirmed these readings. The recording sync stays unverified '
                     'because drift was not checked.')
    else:
        parts.append('Sync stays unverified until a person confirms the frames they read.')
    return ' '.join(parts)


def calibrate_offset(offset_s, checkpoints, human_confirmed=False):
    """Corrected offset from one or more (expected_game_time, shown_game_time) pairs.

    Each pair may be a tuple or a dict with those two keys. Expected is the
    game time the current offset assigns (a label, so it may be negative).
    Shown is the clock on the frame and cannot be negative. The correction
    for a pair is expected minus shown. The returned offset_s is the current
    offset plus the mean of those corrections.

    spread_s is max(correction) minus min(correction). With one checkpoint
    it is 0 and agreement is not_measured. With more, agreement is
    within_precision when the spread is at or under the coarsest reading
    grain, and disagreement when it is wider. precision is whole_second,
    subsecond, or mixed, from how the clocks were written.

    verified is false. drift_checked is false. human_confirmed is false
    unless the caller passes True after a person has read the frames, and
    True is rejected when the checkpoints disagree. Confirmation does not
    measure drift. No constant is added for a loading screen.
    """
    if type(human_confirmed) is not bool:
        raise ValueError('human_confirmed must be true or false')
    if isinstance(checkpoints, (str, bytes)) or isinstance(checkpoints, dict):
        raise ValueError('checkpoints must be a list of (expected_game_time, shown_game_time) pairs')
    try:
        items = list(checkpoints)
    except TypeError as exc:
        raise ValueError('checkpoints must be a list of (expected_game_time, shown_game_time) pairs') from exc
    if not items:
        raise ValueError('At least one checkpoint is required')
    current = _require_offset(offset_s)
    rows = []
    for item in items:
        expected_raw, shown_raw = _checkpoint_pair(item)
        expected = parse_game_time(expected_raw, 'Expected game time', allow_negative=True)
        shown = parse_game_time(shown_raw, 'Shown game time', allow_negative=False)
        correction = _clean(expected['seconds'] - shown['seconds'])
        kinds = {expected['precision'], shown['precision']}
        rows.append(dict(
            expected_game_s=expected['seconds'],
            shown_game_s=shown['seconds'],
            correction_s=correction,
            precision=_combined_precision(kinds),
            resolution_s=max(expected['resolution_s'], shown['resolution_s']),
        ))
    corrections = [row['correction_s'] for row in rows]
    correction_s = _clean(sum(corrections) / len(corrections))
    corrected = _clean(current + correction_s)
    spread_s = _clean(max(corrections) - min(corrections))
    precision = _combined_precision({row['precision'] for row in rows})
    resolution_s = max(row['resolution_s'] for row in rows)
    if len(rows) < 2:
        agreement = 'not_measured'
    elif spread_s > resolution_s + _GRAIN_EPS_S:
        agreement = 'disagreement'
    else:
        agreement = 'within_precision'
    if human_confirmed and agreement == 'disagreement':
        raise ValueError(
            f'Checkpoints disagree by {_fmt(spread_s)}s, more than the {_fmt(resolution_s)}s grain. '
            'Confirming them does not yield one offset')
    for row in rows:
        implied_video_s = video_seconds(row['expected_game_s'], current)
        labelled = game_seconds(implied_video_s, corrected)
        row['implied_video_s'] = _clean(implied_video_s)
        row['residual_s'] = _clean(labelled - row['shown_game_s'])
    return dict(
        offset_s=corrected,
        current_offset_s=current,
        correction_s=correction_s,
        corrections_s=corrections,
        spread_s=spread_s,
        n_checkpoints=len(rows),
        precision=precision,
        resolution_s=resolution_s,
        agreement=agreement,
        status='human_confirmed' if human_confirmed else 'unverified',
        human_confirmed=human_confirmed,
        drift_checked=False,
        verified=False,
        checkpoints=rows,
        note=_note(corrected, current, correction_s, spread_s, len(rows),
                   precision, resolution_s, agreement, human_confirmed),
    )


def main(argv=None):
    """Print the calibration report as JSON. No files, footage, or network."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--offset', type=float, required=True,
                        help='Current offset in seconds (video = game + offset). '
                             'Negative values need --offset=-95.5.')
    parser.add_argument('--checkpoint', nargs=2, action='append', required=True,
                        metavar=('EXPECTED', 'SHOWN'),
                        help='Expected game time, then the clock on that frame. '
                             'Seconds or M:SS. Repeat for another frame. '
                             'Example: --checkpoint 5:00 4:58 adds 2s. '
                             'Video 10:00 vs HUD 8:46 is --checkpoint 10:00 8:46.')
    parser.add_argument('--confirm', action='store_true',
                        help='Record that a person read these frames. '
                             'Does not measure drift or set verified.')
    args = parser.parse_args(argv)
    try:
        report = calibrate_offset(args.offset, args.checkpoint, human_confirmed=args.confirm)
    except ValueError as exc:
        parser.exit(1, f'Calibration: {exc}\n')
    print(json.dumps(report, indent=2))
    if report['agreement'] == 'disagreement':
        parser.exit(DISAGREEMENT_EXIT)


if __name__ == '__main__':
    main()
