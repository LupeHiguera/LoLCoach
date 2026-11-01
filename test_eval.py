import copy
import errno
import io
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from coach import cloud, contracts, eval as evaluation, event_eval, harness
from test_harness import PNG

EXAMPLES = Path(__file__).parent / 'coach' / 'examples'


def answer(context):
    """Contract-valid stub. Any stored observation is cited; events alone abstain."""
    moment = context['packet']['moment_id']
    observations = context['observations']['observations']
    if observations:
        ref = observations[0]['id']
        return dict(schema_version=1, moment_id=moment, assessment='reviewable',
                    claims=[dict(statement='The cited fixture evidence supports a bounded review.',
                                 kind='hypothesis', evidence_refs=[ref])],
                    alternative=dict(action='Hold the position described by the cited evidence.',
                                     tradeoff='Give up pressure.', evidence_refs=[ref]),
                    practice_focus='Review this fabricated situation once.',
                    missing_evidence=['Cooldowns are unknown.'])
    return dict(schema_version=1, moment_id=moment, assessment='needs_more_evidence', claims=[],
                alternative=None, practice_focus=None, missing_evidence=['No observation evidence was supplied.'])


def envelope(result, status='completed'):
    return dict(status=status, model=cloud.MODEL,
                usage=dict(input_tokens=1000, output_tokens=400,
                           input_tokens_details=dict(cached_tokens=200),
                           output_tokens_details=dict(reasoning_tokens=300)),
                output=[dict(type='reasoning', summary=[]),
                        dict(type='message', content=[dict(type='output_text', text=json.dumps(result))])])


class DatasetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        shutil.copytree(EXAMPLES, self.root / 'examples')
        self.dataset = self.root / 'examples' / 'eval_dataset.json'
        self.output = self.root / 'run'

    def change_dataset(self, change):
        data = harness.read_json(self.dataset)
        change(data)
        self.dataset.write_text(json.dumps(data))

    def approve(self):
        data, cases = evaluation.load_dataset(self.dataset)
        for spec, case in zip(data['cases'], cases):
            spec['cloud_approved_sha256'] = cloud.fingerprint(cloud.checked_text(case['packet'], case['observations']))
        self.dataset.write_text(json.dumps(data))

    def selected(self, split='test'):
        _, cases = evaluation.load_dataset(self.dataset)
        if split == 'all':
            return cases
        return [case for case in cases if case['spec']['split'] == split]

    def assert_completed_test_split(self, summary):
        cases = self.selected()
        labelled = sum(case['spec']['expected_assessment'] is not None for case in cases)
        self.assertEqual(summary['counts']['ok'], len(cases))
        self.assertEqual(summary['assessment_labelled_ok'], labelled)
        self.assertEqual(summary['assessment_matches'], labelled)

    def run_local(self, response=None):
        calls = []

        def complete(_, request, with_metadata, timeout_s):
            self.assertTrue(with_metadata)
            self.assertEqual(timeout_s, harness.MODEL_TIMEOUT_S)
            calls.append(request)
            context = json.loads(request['messages'][1]['content'][0]['text'])
            result = answer(context) if response is None else response
            return dict(result=result, usage=None, served_model='stub-local')

        with mock.patch.object(harness, 'complete', side_effect=complete), redirect_stdout(io.StringIO()):
            result = evaluation.run_dataset(self.dataset, self.output, 'local', 'stub-local')
        return result, calls

    def test_dataset_rejects_unknown_fields_duplicate_ids_and_game_split_leakage(self):
        original = harness.read_json(self.dataset)
        for kind in ('unknown', 'duplicate', 'split', 'path'):
            data = copy.deepcopy(original)
            if kind == 'unknown':
                data['cases'][0]['raw_match'] = {}
            elif kind == 'duplicate':
                data['cases'][1]['id'] = data['cases'][0]['id']
            elif kind == 'split':
                data['cases'][1]['group'] = data['cases'][0]['group']
                data['cases'][1]['split'] = 'dev'
            else:
                data['cases'][0]['packet'] = '../../outside.json'
            self.dataset.write_text(json.dumps(data))
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                evaluation.load_dataset(self.dataset)

    def test_dataset_paths_cannot_escape_through_symlinks(self):
        (self.root / 'secret.json').write_text('{}')
        try:
            (self.dataset.parent / 'escape.json').symlink_to(self.root / 'secret.json')
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS) or getattr(exc, 'winerror', None) == 1314:
                self.skipTest('This account cannot create symlinks')
            raise
        self.change_dataset(lambda d: d['cases'][0].update(packet='escape.json'))
        with self.assertRaisesRegex(ValueError, 'escapes'):
            evaluation.load_dataset(self.dataset)

    def test_run_report_and_resume_do_not_repeat_requests_or_send_labels(self):
        summary, calls = self.run_local()
        self.assert_completed_test_split(summary)
        self.assertTrue(summary['synthetic'])
        for request in calls:
            self.assertEqual(len(request['messages']), 2)
            context = json.loads(request['messages'][1]['content'][0]['text'])
            self.assertEqual(set(context), {'packet', 'observations'})
            self.assertNotIn('frames', context['packet'])
            self.assertNotIn('expected_assessment', json.dumps(context))
        again, calls = self.run_local()
        self.assertEqual(calls, [])
        self.assert_completed_test_split(again)
        self.assertFalse((self.output / '.lock').exists())

    def test_changed_evidence_or_prompt_requires_another_run(self):
        self.run_local()
        with mock.patch.object(harness, 'REVIEW_PROMPT', 'New prompt'), self.assertRaisesRegex(ValueError, 'changed'):
            self.run_local()
        path = self.dataset.parent / 'observations.json'
        data = harness.read_json(path)
        data['unknowns'].append('Another unknown.')
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.run_local()

    def test_bad_output_is_failure_and_is_not_retried(self):
        count = len(self.selected())
        summary, calls = self.run_local(response={})
        self.assertEqual(summary['counts']['failed'], count)
        self.assertEqual(len(calls), count)
        _, calls = self.run_local()
        self.assertEqual(calls, [])
        row = harness.read_json(evaluation.result_path(self.output, 'missing-footage'))
        self.assertIsNone(row['result'])
        self.assertIsNotNone(row['error'])

    def test_ratings_bind_to_saved_results_and_reject_incomplete_scores(self):
        self.run_local()
        sheet = evaluation.rating_sheet(self.dataset, self.output)
        self.assertEqual(evaluation.summarize_ratings(sheet, self.output)['rated_cases'], 0)
        for row in sheet['ratings']:
            row['scores'] = {key: 2 for key in evaluation.RUBRIC}
            row['unsupported_claims'] = 0
        summary = evaluation.summarize_ratings(sheet, self.output)
        self.assertEqual(summary['rated_cases'], len(self.selected()))
        self.assertEqual(summary['mean_scores']['factual_support'], 2)
        sheet['ratings'][0]['scores']['uncertainty'] = True
        with self.assertRaises(ValueError):
            evaluation.summarize_ratings(sheet, self.output)
        sheet['ratings'][0]['scores']['uncertainty'] = 2
        sheet['ratings'][0]['result']['moment_id'] = 'changed'
        with self.assertRaisesRegex(ValueError, 'differs'):
            evaluation.summarize_ratings(sheet, self.output)

    def test_report_rejects_a_different_dataset(self):
        self.run_local()
        self.change_dataset(lambda d: d.update(id='another-dataset'))
        with self.assertRaisesRegex(ValueError, 'does not match'):
            evaluation.report(self.dataset, self.output)

    def test_comparison_requires_identical_cases_and_includes_ratings(self):
        self.run_local()
        other = self.root / 'other-run'
        self.output, original = other, self.output
        self.run_local()
        comparison = evaluation.compare(self.dataset, [original, other])
        self.assertEqual(len(comparison['comparisons']), 2)
        sheet = evaluation.rating_sheet(self.dataset, other)
        (other / 'ratings.json').write_text(json.dumps(sheet))
        comparison = evaluation.compare(self.dataset, [original, other])
        self.assertEqual(comparison['comparisons'][1]['human_ratings']['rated_cases'], 0)
        with self.assertRaises(ValueError):
            evaluation.compare(self.dataset, [original])
        with mock.patch.object(harness, 'complete', return_value=dict(result={}, usage=None, served_model='stub')):
            with redirect_stdout(io.StringIO()):
                evaluation.run_dataset(self.dataset, self.root / 'subset', 'local', 'stub', limit=1)
        with self.assertRaisesRegex(ValueError, 'same cases'):
            evaluation.compare(self.dataset, [original, self.root / 'subset'])

    def test_privacy_approval_does_not_invalidate_a_saved_local_run(self):
        self.run_local()
        self.approve()
        summary, calls = self.run_local()
        self.assertEqual(calls, [])
        self.assert_completed_test_split(summary)

    def test_paid_runs_require_budget_and_exact_human_privacy_hash(self):
        with mock.patch.object(cloud, 'complete') as call:
            for budget in (None, 0, -1, float('nan'), float('inf')):
                with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, 'budget'):
                    evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=budget)
            with self.assertRaisesRegex(ValueError, 'privacy'):
                evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=1)
            call.assert_not_called()
        self.assertFalse(self.output.exists())
        self.approve()
        path = self.dataset.parent / 'observations.json'
        data = harness.read_json(path)
        data['unknowns'].append('Edited after privacy review.')
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'privacy'):
            evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=1)

    def test_cloud_reservations_costs_and_reuse_with_simulated_transport(self):
        self.approve()

        def simulated(request, key):
            self.assertEqual(key, 'dummy-key')
            self.assertFalse(request['store'])
            self.assertNotIn('previous_response_id', request)
            self.assertNotIn('tools', request)
            self.assertNotIn('image', json.dumps(request['input']))
            return envelope(answer(json.loads(request['input'][1]['content'])))

        with mock.patch.object(cloud, 'load_key', return_value='dummy-key'), \
                mock.patch.object(cloud, 'complete', side_effect=simulated) as call, redirect_stdout(io.StringIO()):
            count = len(self.selected())
            summary = evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=1)
            self.assertEqual(call.call_count, count)
            self.assertEqual(summary['counts']['ok'], count)
            self.assertAlmostEqual(summary['estimated_cost_usd'], count * 0.00562)
            self.assertLess(summary['reserved_usd'], 1)
            evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=1)
            self.assertEqual(call.call_count, count)

    def test_tiny_budget_sends_nothing(self):
        self.approve()
        with mock.patch.object(cloud, 'load_key') as load, mock.patch.object(cloud, 'complete') as call:
            summary = evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=0.0001)
            self.assertEqual(summary['counts']['not_run'], len(self.selected()))
            self.assertEqual(summary['reserved_usd'], 0)
            call.assert_not_called()
            load.assert_not_called()

    def test_cached_cloud_run_does_not_need_a_key(self):
        self.approve()

        def simulated(request, _):
            return envelope(answer(json.loads(request['input'][1]['content'])))

        with mock.patch.object(cloud, 'load_key', return_value='dummy-key'), \
                mock.patch.object(cloud, 'complete', side_effect=simulated), redirect_stdout(io.StringIO()):
            evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=1)
        with mock.patch.object(cloud, 'load_key') as load, mock.patch.object(cloud, 'complete') as call:
            summary = evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=1)
            self.assertEqual(summary['counts']['ok'], len(self.selected()))
            load.assert_not_called()
            call.assert_not_called()

    def test_cloud_failures_keep_reservation_and_stop_batch_without_retry(self):
        self.approve()
        with mock.patch.object(cloud, 'load_key', return_value='dummy-key'), \
                mock.patch.object(cloud, 'complete', side_effect=ValueError('private server error')) as call, \
                redirect_stdout(io.StringIO()):
            summary = evaluation.run_dataset(self.dataset, self.output, 'openai', cloud.MODEL, budget_usd=1)
            self.assertEqual(call.call_count, 1)
            self.assertEqual(summary['counts']['failed'], 1)
            self.assertEqual(summary['unknown_billing_cases'], 1)
            self.assertGreater(summary['reserved_usd'], 0)
            self.assertNotIn('private server error', (evaluation.result_path(self.output, 'missing-footage')).read_text())

    def test_stale_lock_explains_recovery_and_leftover_tmp_is_replaced(self):
        self.output.mkdir()
        (self.output / '.lock').touch()
        with self.assertRaisesRegex(ValueError, 'interrupted'):
            self.run_local()
        (self.output / '.lock').unlink()
        self.run_local()
        (self.output / 'report.json.tmp').write_text('partial')
        summary, calls = self.run_local()
        self.assertEqual((summary['counts']['ok'], len(calls)), (len(self.selected()), 0))
        self.assertFalse((self.output / 'report.json.tmp').exists())

    def test_pending_request_is_never_retransmitted_on_resume(self):
        self.run_local()
        target = evaluation.result_path(self.output, 'missing-footage')
        row = harness.read_json(target)
        row.update(status='pending', result=None)
        target.write_text(json.dumps(row))
        summary, calls = self.run_local()
        self.assertEqual(calls, [])
        self.assertEqual(summary['counts']['pending'], 1)

    def test_local_end_to_end_over_http_including_vision_frame_transport(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append(request)
                self_test.assertEqual(self.path, '/v1/chat/completions')
                self_test.assertIsNone(self.headers.get('Authorization'))
                context = json.loads(request['messages'][1]['content'][0]['text'])
                if 'observations' in context:
                    result = answer(context)
                else:
                    result = dict(schema_version=1, moment_id=context['packet']['moment_id'],
                                  observations=[], unknowns=['Synthetic single-pixel frames, no gameplay.'])
                response = dict(model='stub', usage=dict(prompt_tokens=10, completion_tokens=20),
                                choices=[dict(finish_reason='stop', message=dict(content=json.dumps(result)))])
                body = json.dumps(response).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self_test = self
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f'http://127.0.0.1:{server.server_port}/v1'
            count = len(self.selected())
            with redirect_stdout(io.StringIO()):
                summary = evaluation.run_dataset(self.dataset, self.output, 'local', 'stub', endpoint)
            self.assertEqual(summary['counts']['ok'], count)
            self.assertEqual(len(requests), count)
            _, cases = evaluation.load_dataset(self.dataset)
            for case in cases:
                root = (self.dataset.parent / case['spec']['packet']).parent
                for frame in case['packet']['frames']:
                    (root / frame['image_file']).write_bytes(PNG)
            with redirect_stdout(io.StringIO()):
                summary = evaluation.run_dataset(self.dataset, self.root / 'vision-run', 'local', 'stub', endpoint, task='observe')
            self.assertEqual(summary['counts']['ok'], count)
            self.assertEqual(len(requests), count * 2)
            self.assertTrue(any(c['type'] == 'image_url' for c in requests[-1]['messages'][1]['content']))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_control_moments_and_pattern_fixtures_stay_one_game_per_split(self):
        """Control review.json withholds advice. Pattern cases are labelled fixtures.

        Agreement counts the assessment field. A stub that adds an alternative
        still matches a reviewable control label. review.json stays out of the request.
        """
        data, cases = evaluation.load_dataset(self.dataset)
        self.assertTrue(data['synthetic'])
        by_id = {case['spec']['id']: case for case in cases}
        self.assertEqual([case['spec']['id'] for case in cases[:2]],
                         ['missing-footage', 'visible-positioning'])
        expected = {
            'pattern-early-river': ('synthetic-game-measured-test', 'test', 'reviewable'),
            'pattern-charm-miss': ('synthetic-game-measured-test', 'test', 'reviewable'),
            'control-safe-recall': ('synthetic-game-measured-test', 'test', 'reviewable'),
            'control-unreadable': ('synthetic-game-measured-test', 'test', 'needs_more_evidence'),
            'control-lane-hold': ('synthetic-game-measured-dev', 'dev', 'reviewable'),
            'pattern-farm-gap': ('synthetic-game-measured-dev', 'dev', 'reviewable'),
        }
        splits = {}
        for case_id, (group, split, label) in expected.items():
            case = by_id[case_id]
            self.assertEqual((case['spec']['group'], case['spec']['split'],
                              case['spec']['expected_assessment']), (group, split, label))
            self.assertEqual(splits.setdefault(group, split), split)
            self.assertNotIn('death-', case['packet']['moment_id'])
            self.assertIn('Fabricated', json.dumps(case['observations']))
            cloud.checked_text(case['packet'], case['observations'])
            deaths = [event for event in case['packet']['events'] if event['me'] == 'death']
            if case_id == 'pattern-early-river':
                self.assertEqual(len(deaths), 1)
                self.assertLess(case['packet']['decision_ms'], 8 * 60 * 1000)
                self.assertEqual(deaths[0]['game_ms'], case['packet']['decision_ms'])
                self.assertIn('river', case['observations']['observations'][0]['statement'])
            else:
                self.assertEqual(deaths, [])
        self.assertEqual(set(splits), {'synthetic-game-measured-test', 'synthetic-game-measured-dev'})
        self.assertEqual({by_id[name]['packet']['opponent'] for name in expected
                          if expected[name][0] == 'synthetic-game-measured-test'}, {'Syndra'})
        self.assertEqual({by_id[name]['packet']['opponent'] for name in expected
                          if expected[name][0] == 'synthetic-game-measured-dev'}, {'Orianna'})
        charm = by_id['pattern-charm-miss']['observations']['observations'][0]
        self.assertEqual(len(charm['evidence_refs']), 2)
        self.assertIn('charm', charm['statement'].lower())
        self.assertEqual(by_id['pattern-farm-gap']['packet']['decision_ms'], 600000)
        self.assertIn('creep', by_id['pattern-farm-gap']['observations']['observations'][0]['statement'])
        for case_id in ('control-safe-recall', 'control-lane-hold', 'control-unreadable'):
            case = by_id[case_id]
            review = harness.read_json(self.dataset.parent / 'measured' / case_id / 'review.json')
            contracts.validate_review(review, case['packet'], case['observations'])
            self.assertEqual(review['assessment'], case['spec']['expected_assessment'])
            self.assertIsNone(review['alternative'])
            self.assertIsNone(review['practice_focus'])
            if review['assessment'] == 'reviewable':
                self.assertIn('does not support a mistake', review['claims'][0]['statement'])
                advised = copy.deepcopy(review)
                advised['practice_focus'] = 'Invent a drill.'
                with self.assertRaisesRegex(ValueError, 'practice focus'):
                    contracts.validate_review(advised, case['packet'], case['observations'])
            else:
                self.assertEqual(review['claims'], [])
                self.assertTrue(review['missing_evidence'])
        for case_id in ('pattern-early-river', 'pattern-charm-miss', 'pattern-farm-gap'):
            self.assertFalse((self.dataset.parent / 'measured' / case_id / 'review.json').exists())

        summary, calls = self.run_local()
        self.assert_completed_test_split(summary)
        saved = harness.read_json(evaluation.result_path(self.output, 'control-safe-recall'))
        gold = harness.read_json(self.dataset.parent / 'measured' / 'control-safe-recall' / 'review.json')
        self.assertEqual(saved['result']['assessment'], gold['assessment'])
        self.assertIsNone(gold['alternative'])
        self.assertIsNotNone(saved['result']['alternative'])
        sent = json.dumps(calls)
        self.assertNotIn('review.json', sent)
        self.assertNotIn('does not support a mistake', sent)
        self.assertNotIn('expected_assessment', sent)

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            evaluation.main(['validate', '--dataset', str(self.dataset)])
        report = json.loads(stdout.getvalue())
        self.assertEqual((report['dataset'], report['synthetic'], report['cases']),
                         (data['id'], True, len(cases)))
        self.assertGreaterEqual(report['cases'], 8)


class ReportTests(unittest.TestCase):
    def test_wilson_interval_is_wide_for_few_cases_and_absent_without_any(self):
        self.assertIsNone(evaluation.wilson_95(0, 0))
        low, high = evaluation.wilson_95(4, 5)
        self.assertLess(low, 0.4)
        self.assertGreater(high, 0.95)
        low, high = evaluation.wilson_95(0, 10)
        self.assertEqual(low, 0.0)
        self.assertGreater(high, 0.25)


class CloudTests(unittest.TestCase):
    def test_privacy_checks_reject_obvious_private_text(self):
        packet = harness.read_json(EXAMPLES / 'packet.json')
        observations = harness.read_json(EXAMPLES / 'observations.json')
        for value in ('player#NA1', '/Users/alice/recording.mp4', r'C:\\secret\\video.mp4',
                      'https://private.example', 'PUUID: hidden', 'data:image/png;base64,private'):
            edited = dict(observations, unknowns=[value])
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'Cloud text'):
                cloud.checked_text(packet, edited)

    def test_cost_does_not_double_bill_reasoning_and_rejects_bad_usage(self):
        response = envelope({})
        self.assertAlmostEqual(cloud.cost_usd(response['usage']), 0.00562)
        self.assertIsNone(cloud.cost_usd(None))
        self.assertIsNone(cloud.cost_usd(dict(input_tokens=True, output_tokens=1)))
        self.assertIsNone(cloud.cost_usd(dict(input_tokens=1, output_tokens=1, input_tokens_details=['invalid'])))

    def test_response_parser_rejects_malformed_envelopes_without_crashing(self):
        responses = (None, dict(status='completed', output=None),
                     dict(status='completed', output=[None]),
                     dict(status='completed', output=[dict(type='message', content='invalid')]),
                     dict(status='completed', output=[dict(type='message', content=[None])]),
                     dict(status='completed', output=[dict(type='message', content=[dict(type='output_text', text={})])]))
        for response in responses:
            with self.subTest(response=response), self.assertRaises(ValueError):
                cloud.parse_result(response)

    def test_response_parser_rejects_refusal_truncation_and_malformed_json(self):
        for response in (envelope({}, 'incomplete'),
                         dict(status='completed', output=[dict(type='message', content=[dict(type='refusal')])]),
                         dict(status='completed', output=[])):
            with self.assertRaises(ValueError):
                cloud.parse_result(response)
        self.assertEqual(cloud.parse_result(envelope({'valid': 'json'})), {'valid': 'json'})

    def test_transport_fixed_endpoint_no_redirects_and_key_not_in_body(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(envelope({})).encode()
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(cloud, 'build_opener', return_value=opener) as builder:
            cloud.complete({'test': 'request'}, 'dummy-key')
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.openai.com/v1/responses')
        self.assertEqual(json.loads(request.data), {'test': 'request'})
        self.assertIsInstance(builder.call_args.args[1], harness.NoRedirect)


class EventTests(unittest.TestCase):
    def timeline(self, events):
        return dict(schema_version=1, moment_id='synthetic-actions', start_ms=0, decision_ms=30000,
                    events=[dict(label=label, actor=actor, game_ms=time) for label, actor, time in events])

    def test_dense_actions_and_duplicate_predictions(self):
        gold = self.timeline([('cast', 'self', i * 50) for i in range(600)])
        found = copy.deepcopy(gold)
        found['events'].append(found['events'][0])
        result = event_eval.score_events(gold, found, 0)
        self.assertEqual((result['tp'], result['fp'], result['fn']), (600, 1, 0))
        self.assertEqual(result['recall'], 1)

    def test_exact_actor_label_and_time_tolerance(self):
        gold = harness.read_json(EXAMPLES / 'events_gold.json')
        found = harness.read_json(EXAMPLES / 'events_predictions.json')
        result = event_eval.score_events(gold, found)
        self.assertEqual((result['tp'], result['fp'], result['fn']), (2, 1, 1))
        self.assertAlmostEqual(result['f1'], 2 / 3)
        found['events'][0]['actor'] = 'enemy-mid'
        self.assertEqual(event_eval.score_events(gold, found)['tp'], 1)
        self.assertEqual(event_eval.score_events(gold, found, 99)['tp'], 0)

    def test_one_to_one_matching_does_not_drop_a_possible_match(self):
        gold = self.timeline([('cast', 'self', 100), ('cast', 'self', 200)])
        found = self.timeline([('cast', 'self', 0), ('cast', 'self', 110)])
        self.assertEqual(event_eval.score_events(gold, found, 100)['tp'], 2)

    def test_empty_labels_do_not_report_perfect_accuracy(self):
        empty = self.timeline([])
        self.assertIsNone(event_eval.score_events(empty, empty)['f1'])

    def test_future_actions_or_mismatched_window_rejected(self):
        gold = self.timeline([('cast', 'self', 100)])
        found = self.timeline([('cast', 'self', 31000)])
        with self.assertRaises(ValueError):
            event_eval.score_events(gold, found)
        found['events'] = []
        found['decision_ms'] = 25000
        with self.assertRaises(ValueError):
            event_eval.score_events(gold, found)
        found['decision_ms'] = 30000
        found['moment_id'] = 'another-moment'
        with self.assertRaises(ValueError):
            event_eval.score_events(gold, found)


if __name__ == '__main__':
    unittest.main()
