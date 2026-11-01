"""One offset predicts a game clock. Agreement at one timestamp stays unverified."""
import math
import unittest

from coach import clock
from coach.clips import plan_clips
from test_clips import MATCH, death


class ClockTests(unittest.TestCase):
    def test_video_time_is_game_time_plus_offset(self):
        self.assertEqual(clock.video_seconds(95.5, -95.5), 0)
        self.assertEqual(clock.game_seconds(0, -95.5), 95.5)
        self.assertEqual(clock.game_seconds(clock.video_seconds(61, -1.6), -1.6), 61)

    def test_practice_tool_checkpoint_agrees_at_whole_seconds_only(self):
        # Video 1:00 showed game 1:01 with the saved -1.6 s offset (PLAN.md).
        self.assertAlmostEqual(clock.game_seconds(60, -1.6), 61.6)
        self.assertTrue(clock.whole_seconds_agree(60, 61, -1.6))
        self.assertFalse(clock.whole_seconds_agree(60, 61, 0))

    def test_one_reading_corrects_the_offset_and_stays_unverified(self):
        # Pause where the offset predicts 5:00 and the HUD shows 4:58.
        corrected = clock.corrected_offset(-296, 300, 298)
        self.assertEqual(corrected, -294)
        video_s = clock.video_seconds(300, -296)
        self.assertEqual(clock.game_seconds(video_s, corrected), 298)
        manifest = plan_clips(MATCH, [death(300000)], corrected, 600)
        self.assertFalse(manifest['sync']['verified'])
        self.assertEqual(manifest['sync']['offset_s'], corrected)

    def test_non_finite_and_negative_readings_are_rejected(self):
        for args in ((True, 0), (float('nan'), 0), (0, float('inf')), ('0', 0)):
            with self.subTest(args=args), self.assertRaises(ValueError):
                clock.game_seconds(*args)
        with self.assertRaises(ValueError):
            clock.corrected_offset(-1.6, -1, 1)
        with self.assertRaises(ValueError):
            clock.whole_seconds_agree(60, -1, -1.6)
        self.assertTrue(math.isfinite(clock.corrected_offset(0, 0, 0)))


if __name__ == '__main__':
    unittest.main()
