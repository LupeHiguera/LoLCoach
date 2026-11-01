"""One recording offset, and the game clock it predicts.

video seconds = game seconds + offset seconds. The inverse predicts the HUD
clock at a video position. One human reading can correct that offset. It does
not measure drift, cuts, or pauses, so nothing here marks sync verified.
"""
import math


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f'{label} must be a finite number')
    return float(value)


def video_seconds(game_s, offset_s):
    """Video position for a game-clock second. Not a check that the file is continuous."""
    return _finite(game_s, 'Game time') + _finite(offset_s, 'Offset')


def game_seconds(video_s, offset_s):
    """Predicted game clock at a video position. A prediction is not a HUD reading.

    review_web uses the same subtraction in predictedGameSeconds.
    """
    return _finite(video_s, 'Video time') - _finite(offset_s, 'Offset')


def corrected_offset(offset_s, expected_game_s, shown_game_s):
    """Offset so the prediction matches one shown clock.

    corrected = offset + (expected − shown), where expected is the clock this
    offset predicted at the paused frame and shown is the clock a person read.
    The result is still one number. Callers keep sync unverified.
    """
    offset = _finite(offset_s, 'Offset')
    expected = _finite(expected_game_s, 'Expected game time')
    shown = _finite(shown_game_s, 'Shown game time')
    if expected < 0 or shown < 0:
        raise ValueError('Game clock readings cannot be negative')
    return offset + (expected - shown)


def whole_seconds_agree(video_s, shown_game_s, offset_s):
    """True when the prediction and the shown clock share a whole second.

    Agreement at one timestamp is not verification.
    """
    shown = _finite(shown_game_s, 'Shown game time')
    if shown < 0:
        raise ValueError('Game clock readings cannot be negative')
    return math.floor(game_seconds(video_s, offset_s)) == math.floor(shown)
