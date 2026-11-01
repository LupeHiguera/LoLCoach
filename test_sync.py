"""Clock-sync helpers. No footage, database, or network."""
import unittest

from coach import clips
from coach.sync import (MEASURED_LOADING_CLOCK_ERROR_S, checkpoint_offset, checkpoint_sync,
                        game_seconds, live_clock_sync, offset_sync, stored_live_offset_s,
                        video_seconds)
from test_clips import MATCH, death


class ClockMappingTests(unittest.TestCase):
    def test_video_and_game_time_are_inverses(self):
        # Recording starts at game 1:35.5, so offset is -95.5 and video 0 is that instant.
        self.assertEqual(video_seconds(95.5, -95.5), 0)
        self.assertEqual(game_seconds(0, -95.5), 95.5)
        self.assertEqual(video_seconds(game_seconds(204.5, -95.5), -95.5), 204.5)
        self.assertEqual(game_seconds(video_seconds(300, -95.5), -95.5), 300)

    def test_checkpoint_matches_the_readme_and_the_measured_loading_miss(self):
        # Expecting 5:00 and seeing 4:58 adds 2 seconds. One frame, not a verified file.
        self.assertEqual(checkpoint_offset(-95.5, 5 * 60, 4 * 60 + 58), -93.5)
        shown = 8 * 60 + 46  # HUD 8:46 at video 10:00
        corrected = checkpoint_offset(0, 10 * 60, shown)
        self.assertEqual(corrected, MEASURED_LOADING_CLOCK_ERROR_S)
        self.assertEqual(corrected, 74)
        self.assertEqual(game_seconds(10 * 60, corrected), shown)
        self.assertEqual(video_seconds(shown, corrected), 10 * 60)
        block = checkpoint_sync(0, 10 * 60, shown)
        self.assertFalse(block['verified'])
        self.assertEqual(block['anchor'], 'checkpoint')

    def test_zero_live_clock_is_not_offset_zero_and_does_not_add_74(self):
        loading = live_clock_sync(0)
        self.assertIsNone(loading['offset_s'])
        self.assertFalse(loading['verified'])
        self.assertEqual(loading['anchor'], 'loading_clock')
        self.assertNotEqual(loading['offset_s'], MEASURED_LOADING_CLOCK_ERROR_S)
        self.assertIn('74', loading['note'])
        self.assertIsNone(live_clock_sync(-1)['offset_s'])
        self.assertIsNone(stored_live_offset_s(0))
        # A manual offset of 0 is a different claim: the caller says video 0 is game 0.
        manual = offset_sync(0)
        self.assertEqual(manual['offset_s'], 0)
        self.assertEqual(manual['anchor'], 'offset')
        self.assertFalse(manual['verified'])

    def test_positive_live_clock_and_storage_rounding_stay_distinct(self):
        self.assertEqual(live_clock_sync(95.5)['offset_s'], -95.5)
        self.assertEqual(stored_live_offset_s(95.5), -95.5)
        self.assertEqual(live_clock_sync(0.04)['anchor'], 'live_clock')
        self.assertEqual(live_clock_sync(0.04)['offset_s'], -0.04)
        self.assertEqual(stored_live_offset_s(0.04), 0)
        self.assertFalse(live_clock_sync(95.5)['verified'])

    def test_non_finite_and_boolean_seconds_rejected(self):
        for call in (lambda: video_seconds(float('nan'), 0),
                     lambda: game_seconds(0, float('inf')),
                     lambda: live_clock_sync(True),
                     lambda: checkpoint_offset(0, False, 1),
                     lambda: stored_live_offset_s(0, ndigits=True)):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()


class PlacementTests(unittest.TestCase):
    def test_manual_zero_offset_still_places_and_stays_unverified(self):
        manifest = clips.plan_clips(MATCH, [death(300000)], 0, 600)
        clip, = manifest['clips']
        self.assertEqual(clip['source_start_s'], 240)
        self.assertFalse(clip['truncated_before'])
        self.assertFalse(clip['truncated_after'])
        self.assertFalse(clip['sync_verified'])
        self.assertFalse(manifest['sync']['verified'])
        self.assertEqual(manifest['sync']['anchor'], 'offset')

    def test_loading_clock_skips_deaths_without_calling_them_truncated(self):
        kill = dict(death(250000), me='kill')
        manifest = clips.plan_clips_from_live_clock(MATCH, [kill, death(300000)], 0, 600)
        self.assertEqual(manifest['clips'], [])
        skip, = manifest['skipped']
        self.assertEqual(skip['death_game_ms'], 300000)
        self.assertFalse(skip['sync_verified'])
        self.assertNotIn('truncated_before', skip)
        self.assertNotIn('truncated_after', skip)
        self.assertIn('not placed', skip['reason'])
        self.assertIsNone(manifest['sync']['offset_s'])
        self.assertFalse(manifest['sync']['verified'])
        self.assertEqual(manifest['sync']['anchor'], 'loading_clock')

    def test_checkpoint_shifts_the_cut_and_live_clock_zero_does_not(self):
        corrected = checkpoint_sync(0, 10 * 60, 8 * 60 + 46)
        placed = clips.plan_clips(MATCH, [death(300000)], corrected['offset_s'], 600, sync=corrected)
        self.assertEqual(placed['clips'][0]['source_start_s'], 240 + 74)
        self.assertEqual(placed['sync']['anchor'], 'checkpoint')
        self.assertFalse(placed['sync']['verified'])
        untouched = clips.plan_clips_from_live_clock(MATCH, [death(300000)], 0, 600)
        self.assertEqual(untouched['clips'], [])

    def test_positive_live_clock_uses_the_same_cut_as_its_offset(self):
        live = clips.plan_clips_from_live_clock(MATCH, [death(300000)], 90, 600)
        manual = clips.plan_clips(MATCH, [death(300000)], -90, 600)
        self.assertEqual(live['clips'][0]['source_start_s'], manual['clips'][0]['source_start_s'])
        self.assertEqual(live['sync']['offset_s'], -90)
        self.assertEqual(live['sync']['anchor'], 'live_clock')
        self.assertFalse(live['sync']['verified'])
        self.assertFalse(live['clips'][0]['sync_verified'])

    def test_outside_and_empty_windows_are_skips_not_truncations(self):
        outside = clips.plan_clips(MATCH, [death(10000)], -20, 280)
        skip, = outside['skipped']
        self.assertNotIn('truncated_before', skip)
        self.assertFalse(skip['sync_verified'])
        empty = clips.review_window(500, -400, 200, 100, 60, 10)
        self.assertEqual(empty['kind'], 'skip')
        self.assertNotIn('truncated_before', empty)
        self.assertFalse(empty['sync_verified'])

    def test_verified_sync_and_loading_block_cannot_place_deaths(self):
        claimed = offset_sync(-90)
        claimed['verified'] = True
        with self.assertRaisesRegex(ValueError, 'verified'):
            clips.plan_clips(MATCH, [death(300000)], -90, 600, sync=claimed)
        loading = live_clock_sync(0)
        with self.assertRaisesRegex(ValueError, 'Loading-clock'):
            clips.plan_clips(MATCH, [death(300000)], 0, 600, sync=loading)


if __name__ == '__main__':
    unittest.main()
