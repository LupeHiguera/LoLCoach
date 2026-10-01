import base64
import copy
import errno
import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from coach import contracts, harness
from coach.clips import plan_clips

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jKxkAAAAASUVORK5CYII=')


def event(time, role=None):
    return dict(time=time, me=role, side='enemy', killer='Akali', victim='Ahri', assists=[], x=5, y=6)


def manifest():
    match = dict(match_id='private-match-id', patch='test', duration_s=600,
                 my_champion='Ahri', opp_champion='Akali', win=0,
                 raw_json='private-puuid-and-name', recording_path='/private/recording.mp4')
    return plan_clips(match, [event(250000), event(300000, 'death'), event(305000)], 0, 600)


def packet(decision=295000):
    return harness.make_packet(manifest(), 'death-300000', decision, 55000, 3)[0]


def observations(p):
    frame = p['frames'][-1]
    return dict(schema_version=1, moment_id=p['moment_id'],
                observations=[dict(id='obs-1', game_ms=frame['game_ms'], statement='Ahri is visible.',
                                   source='local_vision', verification='model_observed', evidence_refs=[frame['id']])],
                unknowns=['Enemy jungler location is not established.'])


def review(p):
    return dict(schema_version=1, moment_id=p['moment_id'], assessment='needs_more_evidence',
                claims=[dict(statement='Ahri is visible.', kind='observation', evidence_refs=['obs-1'])],
                alternative=None, practice_focus=None, missing_evidence=['Inspect the minimap.'])


class MomentTests(unittest.TestCase):
    def test_only_selected_predecision_evidence_and_whitelisted_metadata(self):
        p = packet()
        self.assertEqual([e['game_ms'] for e in p['events']], [250000])
        self.assertTrue(all(p['start_ms'] <= f['game_ms'] <= 295000 for f in p['frames']))
        self.assertNotIn('private', json.dumps(p))
        self.assertNotIn('death-300000', json.dumps(p))
        self.assertFalse(p['sync_verified'])

    def test_window_frames_and_decision_bounds(self):
        for args in [dict(decision_ms=300000), dict(decision_ms=239000),
                     dict(decision_ms=295000, window_ms=60001),
                     dict(decision_ms=295000, frame_count=9), dict(decision_ms=True)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                harness.make_packet(manifest(), 'death-300000', **args)

    def test_many_events_rejected_instead_of_silently_dropped(self):
        data = manifest()
        data['clips'][0]['events'] = [event(250000 + i) for i in range(13)]
        with self.assertRaisesRegex(ValueError, 'too many'):
            harness.make_packet(data, 'death-300000', 295000, 55000)

    def test_knowledge_requires_matching_patch_and_explicit_selection(self):
        selected = [dict(id='rule-1', patch='test', text='Synthetic game rule for testing.')]
        p, _ = harness.make_packet(manifest(), 'death-300000', 295000, knowledge=selected)
        self.assertEqual(p['knowledge'], selected)
        selected[0]['patch'] = 'other-patch'
        with self.assertRaisesRegex(ValueError, 'patch'):
            harness.make_packet(manifest(), 'death-300000', 295000, knowledge=selected)
        with self.assertRaises(ValueError):
            harness.make_packet(manifest(), 'death-300000', 295000, knowledge={})

    def test_prepare_preview_needs_no_media_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / 'manifest.json'
            source.write_text(json.dumps(manifest()))
            output = root / 'prepared'
            stdout = io.StringIO()
            with redirect_stdout(stdout), mock.patch.object(harness, 'run_media') as run:
                harness.main(['prepare', '--manifest', str(source), '--moment', 'death-300000',
                              '--at-ms', '295000', '--output', str(output), '--dry-run'])
            self.assertFalse(output.exists())
            run.assert_not_called()
            contracts.validate_packet(json.loads(stdout.getvalue()))


class ContractTests(unittest.TestCase):
    def test_rejects_history_private_fields_future_evidence_and_bool_timestamps(self):
        for change in ('history', 'raw_json', 'future', 'bool', 'duplicate'):
            p = packet()
            if change in ('history', 'raw_json'):
                p[change] = []
            elif change == 'future':
                p['frames'][0]['game_ms'] = 310000
            elif change == 'bool':
                p['decision_ms'] = True
            else:
                p['frames'][1]['id'] = p['frames'][0]['id']
            with self.subTest(change=change), self.assertRaises(ValueError):
                contracts.validate_packet(p)

    def test_observation_citations_time_and_verification_are_checked(self):
        p = packet()
        for change in ('reference', 'future', 'verified', 'other_moment', 'collision'):
            o = observations(p)
            if change == 'reference':
                o['observations'][0]['evidence_refs'] = ['invented-frame']
            elif change == 'future':
                o['observations'][0]['game_ms'] = 310000
            elif change == 'verified':
                o['observations'][0]['verification'] = 'human_verified'
            elif change == 'other_moment':
                o['moment_id'] = 'another-moment'
            else:
                o['observations'][0]['id'] = p['frames'][0]['id']
            with self.subTest(change=change), self.assertRaises(ValueError):
                contracts.validate_observations(o, p, model_output=True)

    def test_stored_evidence_can_be_human_verified_but_model_output_cannot(self):
        p = packet()
        o = observations(p)
        o['observations'][0]['verification'] = 'human_verified'
        contracts.validate_observations(o, p)
        with self.assertRaises(ValueError):
            contracts.validate_observations(o, p, model_output=True)
        o['observations'][0]['source'] = 'human'
        contracts.validate_observations(o, p)
        o['observations'][0]['verification'] = 'model_observed'
        with self.assertRaisesRegex(ValueError, 'human_verified'):
            contracts.validate_observations(o, p)

    def test_output_limit_and_reasoning_effort_are_explicit(self):
        p = packet()
        payload = harness.build_request('review', p, 'local-test', observations(p))
        self.assertEqual(payload['max_tokens'], harness.MAX_OUTPUT_TOKENS)
        self.assertNotIn('reasoning_effort', payload)
        payload = harness.build_request('review', p, 'local-test', observations(p),
                                        max_tokens=4096, reasoning_effort='low')
        self.assertEqual((payload['max_tokens'], payload['reasoning_effort']), (4096, 'low'))
        for bad in (dict(max_tokens=10), dict(reasoning_effort='xhigh')):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                harness.build_request('review', p, 'local-test', observations(p), **bad)

    def test_uncertainty_cannot_be_published_as_advice(self):
        p = packet()
        r = review(p)
        contracts.validate_review(r, p, observations(p))
        r['practice_focus'] = 'Move backwards.'
        with self.assertRaisesRegex(ValueError, 'Insufficient'):
            contracts.validate_review(r, p, observations(p))

    def test_review_requires_actual_evidence_not_only_rules(self):
        p = packet()
        p['knowledge'] = [dict(id='rule-1', patch='general', text='Synthetic test rule.')]
        r = review(p)
        r['claims'][0]['evidence_refs'] = ['rule-1']
        with self.assertRaisesRegex(ValueError, 'moment evidence'):
            contracts.validate_review(r, p, observations(p))

    def test_supported_alternative_requires_tradeoff_and_citations(self):
        p = packet()
        r = review(p)
        r.update(assessment='reviewable', missing_evidence=[], practice_focus='Synthetic test focus.',
                 alternative=dict(action='Inspect the visible scene.', tradeoff='Costs time.', evidence_refs=['obs-1']))
        contracts.validate_review(r, p, observations(p))
        r['alternative']['evidence_refs'] = ['invented']
        with self.assertRaisesRegex(ValueError, 'evidence'):
            contracts.validate_review(r, p, observations(p))


class RequestTests(unittest.TestCase):
    def test_repeated_requests_never_accumulate_history_or_expose_paths(self):
        first, second = packet(), packet(290000)
        one = harness.build_request('review', first, 'local-test', observations(first))
        two = harness.build_request('review', second, 'local-test', observations(second))
        self.assertEqual([m['role'] for m in two['messages']], ['system', 'user'])
        self.assertNotIn(first['moment_id'], json.dumps(two))
        self.assertNotIn('image_file', json.dumps(two))
        self.assertNotIn('image_url', json.dumps(two))
        self.assertEqual(one['response_format']['type'], 'json_schema')
        self.assertNotIn('previous_response_id', two)

    def test_visual_preview_has_only_selected_frames_and_no_api_events(self):
        p = packet()
        request = harness.build_request('observe', p, 'vision-test', preview=True)
        content = request['messages'][1]['content']
        self.assertEqual(len([c for c in content if c['type'] == 'image_url']), 3)
        self.assertEqual(json.loads(content[0]['text'])['packet']['events'], [])
        self.assertNotIn('data:image', json.dumps(request))

    def test_budget_exceeded_rejects_input_without_truncation(self):
        p = packet()
        original = copy.deepcopy(p)
        with mock.patch.object(harness, 'MAX_TEXT_CHARS', 100), self.assertRaisesRegex(ValueError, 'text characters'):
            harness.build_request('review', p, 'local-test', observations(p))
        self.assertEqual(p, original)

    def test_paths_and_images_cannot_escape_or_exceed_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frame = packet()['frames'][0]
            image = root / frame['image_file']
            image.write_bytes(PNG)
            self.assertTrue(harness.image_data(root, frame).startswith('data:image/png;base64,'))
            image.write_bytes(b'x' * (harness.MAX_IMAGE_BYTES + 1))
            with self.assertRaisesRegex(ValueError, '1 MiB'):
                harness.image_data(root, frame)

    def test_image_symlink_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            frame = packet()['frames'][0]
            image = root / frame['image_file']
            try:
                image.symlink_to(root.parent / 'outside.png')
            except OSError as exc:
                if exc.errno in (errno.EPERM, errno.EACCES, errno.ENOSYS) or getattr(exc, 'winerror', None) == 1314:
                    self.skipTest('This account cannot create symlinks')
                raise
            with self.assertRaisesRegex(ValueError, 'escapes'):
                harness.image_data(root, frame)

    def test_failed_frame_batch_does_not_publish_partial_bundle(self):
        p, clip = harness.make_packet(manifest(), 'death-300000', 295000, frame_count=2)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / clip['file']).write_bytes(b'fixture')
            output = root / 'bundle'
            calls = []
            def extract(command):
                calls.append(command)
                if len(calls) == 2:
                    raise ValueError('decode failed')
                Path(command[-1]).write_bytes(PNG)
            with mock.patch.object(harness, 'run_media', side_effect=extract):
                with self.assertRaisesRegex(ValueError, 'decode failed'):
                    harness.prepare_bundle(root / 'manifest.json', p, clip, output)
            self.assertFalse(output.exists())
            self.assertEqual(float(calls[0][calls[0].index('-ss') + 1]),
                             (p['start_ms'] - clip['game_start_ms']) / 1000)
            self.assertFalse(list(root.glob('.moment-*')))


class LocalServerTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.mode = 'normal'
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path, payload, dict(self.headers)))
                if owner.mode == 'redirect':
                    self.send_response(302)
                    self.send_header('Location', owner.base + '/leaked')
                    self.end_headers()
                    return
                p = json.loads(payload['messages'][1]['content'][0]['text'])['packet']
                if payload['response_format']['json_schema']['name'] == 'observe':
                    value = observations(p)
                    if owner.mode == 'bad_ref':
                        value['observations'][0]['evidence_refs'] = ['invented-frame']
                else:
                    value = review(p)
                body = dict(choices=[dict(finish_reason='length' if owner.mode == 'truncated' else 'stop',
                                         message=dict(content=json.dumps(value)))])
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_port}/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def test_loopback_observe_review_roundtrip_ignores_proxy_and_credentials(self):
        p = packet()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for frame in p['frames']:
                (root / frame['image_file']).write_bytes(PNG)
            with mock.patch.dict(os.environ, HTTP_PROXY='http://127.0.0.1:1', http_proxy='http://127.0.0.1:1',
                                 NO_PROXY='', no_proxy='', OPENAI_API_KEY='must-not-be-read'):
                observed = harness.complete(self.base, harness.build_request('observe', p, 'vision-test', root=root))
                contracts.validate_observations(observed, p)
                reviewed = harness.complete(self.base, harness.build_request('review', p, 'coach-test', observed))
                contracts.validate_review(reviewed, p, observed)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0], '/v1/chat/completions')
        self.assertNotIn('Authorization', self.calls[0][2])
        self.assertIn('data:image/png;base64', json.dumps(self.calls[0][1]))
        self.assertNotIn('image_url', json.dumps(self.calls[1][1]))

    def test_cloud_named_hosts_and_credentials_rejected_before_network(self):
        for url in ['https://api.openai.com/v1', 'http://localhost:1234/v1',
                    'http://192.168.1.2:1234/v1', 'http://user:pw@127.0.0.1:1234/v1',
                    self.base + '?forward=cloud', 'http://127.0.0.1/v1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                harness.complete(url, {})
        self.assertEqual(self.calls, [])

    def test_redirects_are_never_followed(self):
        self.mode = 'redirect'
        p = packet()
        with self.assertRaisesRegex(ValueError, 'HTTP 302'):
            harness.complete(self.base, harness.build_request('review', p, 'local-test', observations(p)))
        self.assertEqual(len(self.calls), 1)

    def test_truncated_answer_is_rejected(self):
        self.mode = 'truncated'
        p = packet()
        with self.assertRaisesRegex(ValueError, "finish_reason='length'"):
            harness.complete(self.base, harness.build_request('review', p, 'local-test', observations(p)))

    def test_invalid_model_output_is_not_saved_by_cli(self):
        self.mode = 'bad_ref'
        p = packet()
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source, output = root / 'packet.json', root / 'observations.json'
            source.write_text(json.dumps(p))
            for frame in p['frames']:
                (root / frame['image_file']).write_bytes(PNG)
            with mock.patch('sys.stderr', new=io.StringIO()), self.assertRaises(SystemExit) as exc:
                harness.main(['observe', '--packet', str(source), '--model', 'local-test',
                              '--base-url', self.base, '--output', str(output)])
            self.assertEqual(exc.exception.code, 1)
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
