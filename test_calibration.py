"""Offset calibration from fabricated clock checkpoints. No footage, database, or network."""
import inspect
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout

from coach import calibration


def assert_unverified(test, report):
    test.assertFalse(report['verified'])
    test.assertFalse(report['drift_checked'])
    note = report['note'].lower()
    test.assertIn('drift', note)
    test.assertIn('unverified', note)
    test.assertIn('not a confidence interval', note)
    test.assertIn('not detected', note)


class ConversionTests(unittest.TestCase):
    def test_local_mapping_matches_the_public_mapping(self):
        # Recording starts at game 1:35.5, so offset is -95.5 and video 0 is that instant.
        self.assertEqual(calibration.video_seconds(95.5, -95.5), 0)
        self.assertEqual(calibration.game_seconds(0, -95.5), 95.5)
        self.assertEqual(calibration.video_seconds(95.5, -95.5),
                         calibration._video_seconds_local(95.5, -95.5))
        self.assertEqual(calibration.game_seconds(60, -1.6),
                         calibration._game_seconds_local(60, -1.6))
        self.assertEqual(calibration.expected_game_seconds(10 * 60, 0), 600)
        self.assertEqual(calibration.expected_game_seconds(60, -1.6), 61.6)

    def test_non_finite_and_boolean_seconds_rejected(self):
        for call in (lambda: calibration.video_seconds(True, 0),
                     lambda: calibration.game_seconds(0, float('nan')),
                     lambda: calibration.video_seconds(0, float('inf')),
                     lambda: calibration._video_seconds_local(False, 0)):
            with self.subTest(call=call), self.assertRaises(ValueError):
                call()


class ParseTests(unittest.TestCase):
    def test_clock_strings_and_written_fractions(self):
        self.assertEqual(calibration.parse_game_time('5:00', 'Expected'),
                         dict(seconds=300.0, precision='whole_second', resolution_s=1.0))
        self.assertEqual(calibration.parse_game_time('4:58', 'Shown'),
                         dict(seconds=298.0, precision='whole_second', resolution_s=1.0))
        self.assertEqual(calibration.parse_game_time('8:46', 'Shown')['seconds'], 8 * 60 + 46)
        self.assertEqual(calibration.parse_game_time(' 10:00 ', 'Expected')['seconds'], 600)
        self.assertEqual(calibration.parse_game_time('1:35.5', 'Game')['seconds'], 95.5)
        fine = calibration.parse_game_time('4:58.20', 'Shown')
        self.assertEqual(fine['seconds'], 298.2)
        self.assertEqual(fine['precision'], 'subsecond')
        self.assertEqual(fine['resolution_s'], 0.01)
        tenth = calibration.parse_game_time('4:58.0', 'Shown')
        self.assertEqual(tenth['precision'], 'subsecond')
        self.assertEqual(tenth['resolution_s'], 0.1)
        self.assertEqual(calibration.parse_game_time(298.0, 'Shown')['precision'], 'whole_second')
        self.assertEqual(calibration.parse_game_time(298, 'Shown')['precision'], 'whole_second')
        self.assertEqual(calibration.parse_game_time('300', 'Expected')['precision'], 'whole_second')
        self.assertEqual(calibration.parse_game_time('300.0', 'Expected')['precision'], 'subsecond')
        self.assertEqual(calibration.parse_game_time('0:10:00', 'Expected')['seconds'], 600)
        self.assertEqual(calibration.parse_game_time('0:08:46', 'Shown')['seconds'], 526)
        self.assertEqual(calibration.parse_game_time('65:00', 'Expected')['seconds'], 3900)
        self.assertEqual(calibration.parse_game_time('1:05:00', 'Expected')['seconds'], 3900)

    def test_invalid_clocks_rejected(self):
        for value in ('4:60', '1:60:00', '', '  ', 'nope', '8 : 46', True, False,
                      None, float('nan'), float('inf'), -1, '-1', '1:00:'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                calibration.parse_game_time(value, 'Shown')
        self.assertEqual(calibration.parse_game_time(-20, 'Expected', allow_negative=True)['seconds'], -20)
        with self.assertRaisesRegex(ValueError, 'negative'):
            calibration.parse_game_time(-20, 'Shown')


class CalibrateTests(unittest.TestCase):
    def test_readme_checkpoint_adds_two_seconds(self):
        report = calibration.calibrate_offset(-95.5, [('5:00', '4:58')])
        self.assertEqual(report['offset_s'], -93.5)
        self.assertEqual(report['current_offset_s'], -95.5)
        self.assertEqual(report['correction_s'], 2)
        self.assertEqual(report['corrections_s'], [2])
        self.assertEqual(report['spread_s'], 0)
        self.assertEqual(report['n_checkpoints'], 1)
        self.assertEqual(report['precision'], 'whole_second')
        self.assertEqual(report['resolution_s'], 1)
        self.assertEqual(report['agreement'], 'not_measured')
        self.assertEqual(report['status'], 'unverified')
        self.assertFalse(report['human_confirmed'])
        point = report['checkpoints'][0]
        self.assertEqual(point['expected_game_s'], 300)
        self.assertEqual(point['shown_game_s'], 298)
        self.assertEqual(point['residual_s'], 0)
        self.assertEqual(point['implied_video_s'], 300 - 95.5)
        self.assertEqual(calibration.video_seconds(298, report['offset_s']), point['implied_video_s'])
        assert_unverified(self, report)
        self.assertIn('fractional', report['note'])
        self.assertIn('nothing to disagree', report['note'])

    def test_loading_clock_miss_is_an_input_pair_not_a_constant(self):
        # Video 10:00 with offset 0 labels game 10:00; the HUD showed 8:46. +74 only for this pair.
        expected = calibration.expected_game_seconds(10 * 60, 0)
        shown = calibration.parse_game_time('8:46', 'Shown')['seconds']
        self.assertEqual((expected, shown), (600, 526))
        report = calibration.calibrate_offset(0, [(expected, shown)])
        self.assertEqual(report['offset_s'], 74)
        self.assertEqual(report['correction_s'], 74)
        self.assertEqual(report['checkpoints'][0]['residual_s'], 0)
        self.assertEqual(calibration.game_seconds(10 * 60, report['offset_s']), shown)
        self.assertEqual(calibration.video_seconds(shown, report['offset_s']), 10 * 60)
        same = calibration.calibrate_offset(0, [('0:10:00', '0:08:46')])
        self.assertEqual(same['correction_s'], 74)
        self.assertEqual(calibration.calibrate_offset(0, [('10:00', '10:00')])['offset_s'], 0)
        self.assertEqual(calibration.calibrate_offset(0, [('0:00', '0:00')])['correction_s'], 0)
        self.assertEqual(calibration.calibrate_offset(0, [('10:00', '9:00')])['correction_s'], 60)
        source = inspect.getsource(calibration.calibrate_offset) + inspect.getsource(calibration._note)
        self.assertNotIn('74', source)
        assert_unverified(self, report)

    def test_whole_second_match_keeps_the_fractional_offset(self):
        # Video 1:00 showing 1:01 agrees with -1.6 only to the displayed second.
        report = calibration.calibrate_offset(-1.6, [('1:01', '1:01')])
        self.assertEqual(report['offset_s'], -1.6)
        self.assertEqual(report['correction_s'], 0)
        self.assertEqual(report['precision'], 'whole_second')
        self.assertIn('fractional', report['note'])
        assert_unverified(self, report)

    def test_shown_clock_ahead_of_expected_subtracts(self):
        report = calibration.calibrate_offset(-95.5, [('5:00', '5:02')])
        self.assertEqual(report['correction_s'], -2)
        self.assertEqual(report['offset_s'], -97.5)

    def test_agreeing_checkpoints_still_do_not_measure_drift(self):
        report = calibration.calibrate_offset(-95.5, [('5:00', '4:58'), ('12:00', '11:58')])
        self.assertEqual(report['corrections_s'], [2, 2])
        self.assertEqual(report['correction_s'], 2)
        self.assertEqual(report['spread_s'], 0)
        self.assertEqual(report['agreement'], 'within_precision')
        self.assertEqual(report['offset_s'], -93.5)
        self.assertEqual(report['n_checkpoints'], 2)
        self.assertEqual([row['residual_s'] for row in report['checkpoints']], [0, 0])
        assert_unverified(self, report)
        self.assertIn('not a measurement of the rest of the file', report['note'])

    def test_one_second_spread_is_inside_a_whole_second_grain(self):
        report = calibration.calibrate_offset(0, [('5:00', '4:58'), ('10:00', '9:57')])
        self.assertEqual(report['corrections_s'], [2, 3])
        self.assertEqual(report['correction_s'], 2.5)
        self.assertEqual(report['spread_s'], 1)
        self.assertEqual(report['agreement'], 'within_precision')
        self.assertEqual(report['precision'], 'whole_second')
        self.assertIn('not the same number', report['note'])
        self.assertEqual([row['residual_s'] for row in report['checkpoints']], [-0.5, 0.5])

    def test_wider_than_a_second_disagrees_and_cannot_be_confirmed(self):
        report = calibration.calibrate_offset(0, [('5:00', '4:58'), ('10:00', '9:56')])
        self.assertEqual(report['corrections_s'], [2, 4])
        self.assertEqual(report['correction_s'], 3)
        self.assertEqual(report['spread_s'], 2)
        self.assertEqual(report['agreement'], 'disagreement')
        self.assertEqual(report['offset_s'], 3)
        self.assertIn('does not fit', report['note'])
        assert_unverified(self, report)
        with self.assertRaisesRegex(ValueError, 'disagree'):
            calibration.calibrate_offset(0, [('5:00', '4:58'), ('10:00', '9:56')],
                                         human_confirmed=True)

    def test_subsecond_grain_and_mixed_precision(self):
        fine = calibration.calibrate_offset(0, [('5:00.0', '4:58.5'), ('6:00.0', '5:58.5')])
        self.assertEqual(fine['precision'], 'subsecond')
        self.assertEqual(fine['resolution_s'], 0.1)
        self.assertEqual(fine['correction_s'], 1.5)
        self.assertEqual(fine['spread_s'], 0)
        self.assertEqual(fine['agreement'], 'within_precision')
        self.assertNotIn('fractional part', fine['note'])
        apart = calibration.calibrate_offset(0, [('5:00.0', '4:58.5'), ('6:00.0', '5:58.3')])
        self.assertEqual(apart['spread_s'], 0.2)
        self.assertEqual(apart['agreement'], 'disagreement')
        self.assertEqual(apart['precision'], 'subsecond')
        mixed = calibration.calibrate_offset(0, [('5:00', '4:58'), ('6:00.0', '5:58.5')])
        self.assertEqual(mixed['precision'], 'mixed')
        self.assertEqual(mixed['resolution_s'], 1)
        self.assertEqual(mixed['spread_s'], 0.5)
        self.assertEqual(mixed['agreement'], 'within_precision')
        self.assertIn('fractional', mixed['note'])
        mixed_apart = calibration.calibrate_offset(0, [('5:00', '4:58'), ('10:00.0', '9:56.0')])
        self.assertEqual(mixed_apart['precision'], 'mixed')
        self.assertEqual(mixed_apart['agreement'], 'disagreement')
        self.assertEqual(mixed_apart['spread_s'], 2)

    def test_three_checkpoints_use_the_mean(self):
        report = calibration.calibrate_offset(0, [('5:00', '4:58'), ('8:00', '7:58'), ('10:00', '9:55')])
        self.assertEqual(report['corrections_s'], [2, 2, 5])
        self.assertEqual(report['correction_s'], 3)
        self.assertEqual(report['spread_s'], 3)
        self.assertEqual(report['n_checkpoints'], 3)
        self.assertEqual(report['agreement'], 'disagreement')

    def test_human_confirmation_does_not_verify_the_file(self):
        report = calibration.calibrate_offset(
            -95.5, [dict(expected_game_time='5:00', shown_game_time='4:58')], human_confirmed=True)
        self.assertEqual(report['offset_s'], -93.5)
        self.assertTrue(report['human_confirmed'])
        self.assertEqual(report['status'], 'human_confirmed')
        self.assertFalse(report['verified'])
        self.assertFalse(report['drift_checked'])
        self.assertIn('confirmed', report['note'])
        self.assertIn('unverified', report['note'])
        self.assertIn('drift', report['note'])

    def test_negative_expected_label_is_allowed(self):
        report = calibration.calibrate_offset(30, [(-20, '0:00')])
        self.assertEqual(report['correction_s'], -20)
        self.assertEqual(report['offset_s'], 10)
        point = report['checkpoints'][0]
        self.assertEqual(point['implied_video_s'], 10)
        self.assertEqual(point['residual_s'], 0)
        self.assertEqual(calibration.game_seconds(10, report['offset_s']), 0)

    def test_rejected_inputs(self):
        pair = [('5:00', '4:58')]
        for args, kwargs in (
                ((float('nan'), pair), {}),
                ((0, []), {}),
                ((0, '5:00'), {}),
                ((0, {'expected_game_time': '5:00', 'shown_game_time': '4:58'}), {}),
                ((0, [('5:00',)]), {}),
                ((0, [('5:00', '4:58', '4:57')]), {}),
                ((0, [dict(expected_game_time='5:00', shown_game_time='4:58', extra=1)]), {}),
                ((True, pair), {}),
                ((0, [(True, '4:58')]), {}),
                ((0, [('5:00', -1)]), {}),
                ((0, [('5:00', '4:60')]), {}),
                ((0, pair), dict(human_confirmed='yes')),
                ((0, pair), dict(human_confirmed=1)),
        ):
            with self.subTest(args=args, kwargs=kwargs), self.assertRaises(ValueError):
                calibration.calibrate_offset(*args, **kwargs)

    def test_report_has_no_identifiers_or_paths(self):
        report = calibration.calibrate_offset(0, [('10:00', '8:46'), ('5:00', '4:58')])
        blob = json.dumps(report).lower()
        for word in ('puuid', 'summoner', 'riot_id', '.mp4', 'password', 'obs_ws'):
            self.assertNotIn(word, blob)


class CliTests(unittest.TestCase):
    def _run(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                calibration.main(argv)
            except SystemExit as exc:
                return exc.code, stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()

    def test_readme_and_loading_pair_print_unverified_json(self):
        code, out, err = self._run(['--offset=-95.5', '--checkpoint', '5:00', '4:58'])
        self.assertEqual(code, 0)
        self.assertEqual(err, '')
        report = json.loads(out)
        self.assertEqual(report['offset_s'], -93.5)
        self.assertEqual(report['status'], 'unverified')
        self.assertFalse(report['verified'])
        code, out, err = self._run(['--offset', '0', '--checkpoint', '10:00', '8:46'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)['offset_s'], 74)
        self.assertEqual(err, '')

    def test_disagreement_exits_after_printing_the_report(self):
        code, out, err = self._run(['--offset', '0', '--checkpoint', '5:00', '4:58',
                                    '--checkpoint', '10:00', '9:56'])
        self.assertEqual(code, calibration.DISAGREEMENT_EXIT)
        self.assertEqual(err, '')
        report = json.loads(out)
        self.assertEqual(report['agreement'], 'disagreement')
        self.assertEqual(report['correction_s'], 3)
        self.assertFalse(report['verified'])

    def test_confirm_flag_and_rejected_clock(self):
        code, out, err = self._run(['--offset=-95.5', '--checkpoint', '5:00', '4:58', '--confirm'])
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report['human_confirmed'])
        self.assertFalse(report['drift_checked'])
        self.assertFalse(report['verified'])
        code, out, err = self._run(['--offset', '0', '--checkpoint', '5:60', '4:58'])
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertIn('seconds must be under 60', err)
        code, out, err = self._run(['--offset', '0', '--checkpoint', '5:00', '4:58',
                                    '--checkpoint', '10:00', '9:56', '--confirm'])
        self.assertEqual(code, 1)
        self.assertEqual(out, '')
        self.assertIn('disagree', err)


if __name__ == '__main__':
    unittest.main()
