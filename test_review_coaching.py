import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import review_coaching
from coach import clips, harness, state
from review_app import ReviewStore, make_handler
from review_data import kill_feed
from test_state import MATCH, build_match

DEATH, DECISION = 118000, 113000


def bundle(conn, match_id=MATCH, death=DEATH, decision=DECISION):
    """A prepared packet for one of this match's deaths, as coach.harness prepare writes it."""
    match = dict(match_id=match_id, patch='test', duration_s=600, my_champion='Ahri', opp_champion='Akali')
    manifest = clips.plan_clips(match, kill_feed(conn, MATCH, 1), 0, 600)
    return harness.make_packet(manifest, f'death-{death}', decision, 30000, 2,
                               state=state.facts_at(conn, MATCH, decision))[0]


def observations(packet):
    frame = packet['frames'][-1]
    return dict(schema_version=1, moment_id=packet['moment_id'], unknowns=['Enemy jungler not on screen.'],
                observations=[dict(id='obs-1', game_ms=frame['game_ms'], statement='Ahri walks into the river.',
                                   source='local_vision', verification='model_observed', evidence_refs=[frame['id']])])


def review(packet):
    return dict(schema_version=1, moment_id=packet['moment_id'], assessment='reviewable',
                claims=[dict(statement='Vi was level 1 and unseen.', kind='hypothesis',
                             evidence_refs=['obs-1', 'riot-player-7'])],
                alternative=dict(action='Ward the river before walking in.', tradeoff='Costs a trinket charge.',
                                 evidence_refs=['obs-1']),
                practice_focus='Ward before entering the river after 1:30.', missing_evidence=[])


def write(folder, **files):
    folder.mkdir(parents=True)
    for name, value in files.items():
        (folder / f'{name}.json').write_text(json.dumps(value), encoding='utf-8')


class CoachingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db, self.moments = root / 'league.db', root / 'moments'
        with closing(sqlite3.connect(self.db)) as conn:
            build_match(conn)
            self.packet = bundle(conn)
            self.other = bundle(conn, match_id='other-match')

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, match_id=MATCH):
        return review_coaching.coaching(self.db, self.moments, match_id)

    def test_review_is_placed_on_its_death_with_resolved_evidence(self):
        write(self.moments / 'decision-1', packet=self.packet, observations=observations(self.packet),
              review=review(self.packet))
        write(self.moments / 'other-game', packet=self.other)
        data = self.load()
        self.assertEqual(data['scanned'], 2)
        [m] = data['moments']
        self.assertEqual((m['bundle'], m['death_ms'], m['decision_ms'], m['start_ms']), ('decision-1', DEATH, DECISION, 83000))
        self.assertTrue(m['state'])
        self.assertIsNone(m['problem'])
        claim = m['review']['claims'][0]
        self.assertEqual([e['source'] for e in claim['evidence']], ['Model observation, unchecked', 'Riot timeline'])
        self.assertTrue(claim['evidence'][1]['text'].startswith('Enemy Vi (JUNGLE): level 1'))
        self.assertEqual(m['review']['alternative']['evidence'][0]['text'], 'Ahri walks into the river.')
        self.assertEqual(m['unknowns'], ['Enemy jungler not on screen.'])

    def test_missing_stages_and_invalid_reviews_are_explained(self):
        write(self.moments / 'a-packet-only', packet=self.packet)
        write(self.moments / 'b-observed', packet=self.packet, observations=observations(self.packet))
        bad = review(self.packet)
        bad['alternative']['evidence_refs'] = ['invented']
        write(self.moments / 'c-invalid', packet=self.packet, observations=observations(self.packet), review=bad)
        problems = [m['problem'] for m in self.load()['moments']]
        self.assertIn('No observations yet', problems[0])
        self.assertIn('No review yet', problems[1])
        self.assertIn('failed validation', problems[2])
        self.assertTrue(all(m['review'] is None for m in self.load()['moments']))

    def test_unplaceable_folders_are_skipped(self):
        (self.moments / 'junk').mkdir(parents=True)
        (self.moments / 'junk' / 'packet.json').write_text('not json')
        write(self.moments / 'wrong-types', packet=dict(self.packet, decision_ms='113000'))
        write(self.moments / 'later-death', packet=dict(self.packet, decision_ms=DEATH + 1))
        (self.moments / 'file.json').write_text('{}')
        self.assertEqual(self.load(), dict(moments=[], scanned=3))

    def test_no_moments_folder_and_unknown_match(self):
        self.assertEqual(self.load(), dict(moments=[], scanned=0))
        with self.assertRaises(KeyError):
            self.load('absent')

    def check(self, bundle='decision-1', observation='obs-1', checked=True, match_id=MATCH):
        return review_coaching.set_checked(self.db, self.moments, match_id, bundle, observation, checked)

    def test_marking_an_observation_checked_and_undoing_it(self):
        write(self.moments / 'decision-1', packet=self.packet, observations=observations(self.packet),
              review=review(self.packet))
        view = self.check()
        self.assertEqual(view['observations'][0]['source'], 'Checked by you')
        self.assertEqual(view['review']['claims'][0]['evidence'][0]['source'], 'Checked by you')
        stored = json.loads((self.moments / 'decision-1' / 'observations.json').read_text(encoding='utf-8'))
        self.assertEqual(stored['observations'][0]['verification'], 'human_verified')
        self.assertEqual(stored['observations'][0]['statement'], 'Ahri walks into the river.')
        self.assertEqual(self.check(checked=False)['observations'][0]['source'], 'Model observation, unchecked')
        self.assertEqual(sorted(p.name for p in (self.moments / 'decision-1').iterdir()),
                         ['observations.json', 'packet.json', 'review.json'])

    def test_check_rejects_escapes_other_games_and_bad_input(self):
        write(self.moments / 'decision-1', packet=self.packet, observations=observations(self.packet))
        write(self.moments / 'other-game', packet=self.other, observations=observations(self.other))
        write(self.moments / 'packet-only', packet=self.packet)
        human = observations(self.packet)
        human['observations'][0].update(source='human', verification='human_verified')
        write(self.moments / 'human', packet=self.packet, observations=human)
        for args, message in ((dict(bundle='../moments/decision-1'), 'Unknown moment bundle'),
                              (dict(bundle='..'), 'Unknown moment bundle'),
                              (dict(bundle='missing'), 'Unknown moment bundle'),
                              (dict(bundle='other-game'), 'another game'),
                              (dict(bundle='packet-only'), 'no observations'),
                              (dict(bundle='human'), 'always checked'),
                              (dict(observation='obs-9'), 'Unknown observation'),
                              (dict(checked='yes'), 'true or false')):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, message):
                self.check(**args)
        with self.assertRaises(KeyError):
            self.check(match_id='absent')

    def test_http_route(self):
        write(self.moments / 'decision-1', packet=self.packet, observations=observations(self.packet),
              review=review(self.packet))
        store = ReviewStore(self.db, Path(self.tmp.name) / 'reviews.db')
        server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store, moments_dir=self.moments))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(f'{base}/api/coaching?id={MATCH}') as r:
                body = json.load(r)
            self.assertEqual(body['moments'][0]['review']['assessment'], 'reviewable')
            self.assertNotIn(self.tmp.name.replace('\\', '\\\\'), json.dumps(body))
            with self.assertRaises(HTTPError) as error:
                urlopen(f'{base}/api/coaching?id=absent')
            self.assertEqual(error.exception.code, 404)
            with urlopen(f'{base}/watch.js') as r:
                self.assertTrue(r.headers['Content-Type'].startswith('application/javascript'))
            body = dict(match_id=MATCH, bundle='decision-1', observation_id='obs-1', checked=True)
            for origin, code in (('https://example.com', 403), (base, 200)):
                req = Request(f'{base}/api/observation-check', data=json.dumps(body).encode(),
                              headers={'Content-Type': 'application/json', 'Origin': origin})
                if code == 200:
                    with urlopen(req) as r:
                        self.assertEqual(json.load(r)['observations'][0]['source'], 'Checked by you')
                else:
                    with self.assertRaises(HTTPError) as error:
                        urlopen(req)
                    self.assertEqual(error.exception.code, code)
            req = Request(f'{base}/api/observation-check', data=json.dumps(dict(body, bundle='..')).encode(),
                          headers={'Content-Type': 'application/json', 'Origin': base})
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    unittest.main()
