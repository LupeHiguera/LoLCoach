"""Video/game clock mapping for one continuous recording.

video seconds = game seconds + offset_s. The offset is a label for that file.
It is not a measurement that the HUD matches, and it does not survive cuts,
pauses or replay-speed changes. verified is always false from these helpers:
nothing here has looked at the picture.

A Live Client gameTime of 0 stays 0:00 through the loading screen. That is not
HUD 0:00. Storing offset 0 in that case claims video 0 is the start of the
game clock. On one ranked recording it was not: video 10:00 showed HUD 8:46,
74 seconds later. Loading length varies. 74 is that one miss, not a number
to add.
"""
import math

# Video 10:00 (600s) showed HUD 8:46 (526s) when offset 0 had been stored from
# a loading-screen gameTime of 0. 600 - 526 = 74. Not a correction constant.
MEASURED_LOADING_CLOCK_ERROR_S = 74
STORAGE_OFFSET_DIGITS = 1

SYNC_NOTE = (
    "video seconds = game seconds + offset_s. Offset is unverified; "
    "check the visible game clock before interpreting a clip. "
    "One offset assumes continuous footage with no cuts or pauses. "
    "A Live Client gameTime of 0 is the loading screen, not HUD 0:00; "
    "treating it as offset 0 was 74s off once (video 10:00 showed game 8:46). "
    "74s is that loading duration, not a correction."
)
LOADING_CLOCK_NOTE = (
    "Live Client gameTime was not positive, so no offset was stored. "
    "gameTime 0 stays 0:00 through the loading screen and is not HUD 0:00. "
    "On one ranked recording, video 10:00 showed game 8:46 (74s). "
    "That measurement is not applied here. Deaths are skipped, not truncated."
)
CHECKPOINT_NOTE = (
    "Offset updated from one paused frame: offset + (expected game seconds - shown HUD seconds). "
    "This lines up that frame only. verified stays false; drift, cuts and pauses are unchecked."
)


def _seconds(value, label):
    """Finite int or float seconds. Booleans are rejected; True is not 1 second."""
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number of seconds")


def video_seconds(game_s, offset_s):
    """Video position for a game-clock time: game_s + offset_s.

    game_s is the labelled game clock, not a pixel read from the HUD.
    The sum is not evidence the frame at that position shows game_s.
    """
    _seconds(game_s, 'Game time')
    _seconds(offset_s, 'Offset')
    return game_s + offset_s


def game_seconds(video_s, offset_s):
    """Game-clock seconds implied by a video position: video_s - offset_s.

    Inverse of video_seconds. The result is the label, not the HUD.
    """
    _seconds(video_s, 'Video time')
    _seconds(offset_s, 'Offset')
    return video_s - offset_s


def offset_sync(offset_s):
    """Sync block for a caller-supplied offset. verified is false.

    offset 0 is allowed: it means the caller claims video 0 is game 0.
    That claim is still unchecked. It is not a Live Client reading; a
    reading of 0 is rejected by live_clock_sync instead of stored as 0.
    """
    _seconds(offset_s, 'Offset')
    return dict(offset_s=offset_s, verified=False, anchor='offset', note=SYNC_NOTE)


def live_clock_sync(game_time_s):
    """Sync block for a Live Client gameTime read at video time 0.

    When game_time_s > 0, offset_s = -game_time_s, so the labelled game
    clock at the start of the file is that reading. One reading does not
    check the HUD later in the file. anchor is live_clock. verified is false.

    When game_time_s <= 0, offset_s is None. None is not 0. A non-positive
    reading is the loading screen (or not a clock), and deaths must not be
    placed as if video time were game time. anchor is loading_clock.
    MEASURED_LOADING_CLOCK_ERROR_S is not added.
    """
    _seconds(game_time_s, 'Live Client gameTime')
    if game_time_s <= 0:
        return dict(offset_s=None, verified=False, anchor='loading_clock', note=LOADING_CLOCK_NOTE)
    return dict(offset_s=-game_time_s, verified=False, anchor='live_clock', note=SYNC_NOTE)


def stored_live_offset_s(game_time_s, ndigits=STORAGE_OFFSET_DIGITS):
    """Offset rounded the way recordings.offset_s is written, or None.

    None means the live clock cannot anchor the file. Do not coerce None
    to 0. Rounding to tenths is storage, not an error bar: a positive
    clock under 0.05s rounds to 0, which is a sub-tenth label, not the
    loading-screen failure. Call live_clock_sync to tell those apart.
    """
    if type(ndigits) is not int or ndigits < 0 or ndigits > 6:
        raise ValueError('Offset storage precision must be 0..6 decimal places')
    sync = live_clock_sync(game_time_s)
    if sync['offset_s'] is None:
        return None
    return round(sync['offset_s'], ndigits)


def checkpoint_offset(offset_s, expected_game_s, shown_game_s):
    """offset_s + (expected_game_s - shown_game_s), from one paused frame.

    expected_game_s is the game time the current offset assigns to the
    frame. shown_game_s is the HUD clock on that frame. Expecting 5:00
    and seeing 4:58 adds 2 seconds. The 74s loading miss is what this
    returns for offset 0, expected 600 and shown 526, and only when those
    two clocks were actually read. The function does not assume them.
    """
    _seconds(offset_s, 'Offset')
    _seconds(expected_game_s, 'Expected game time')
    _seconds(shown_game_s, 'Shown game time')
    return offset_s + (expected_game_s - shown_game_s)


def checkpoint_sync(offset_s, expected_game_s, shown_game_s):
    """Sync block after checkpoint_offset. verified stays false.

    One frame does not verify the rest of the file.
    """
    return dict(offset_s=checkpoint_offset(offset_s, expected_game_s, shown_game_s),
                verified=False, anchor='checkpoint', note=CHECKPOINT_NOTE)
