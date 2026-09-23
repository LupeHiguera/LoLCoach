import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from fetch_matches import SCHEMA, store_match
from review_app import ReviewStore, find_moments, make_handler, static_file
from test_analyze import ME, match as riot_match


def frame(minute, cs, gold=0):
    return dict(time=minute*60000, cs=cs, gold_diff=gold)


class MomentTests(unittest.TestCase):
    def test_no_claim_for_one_snapshot_or_opening(self):
        self.assertEqual(find_moments([frame(0, 0), frame(1, 0), frame(2, 0), frame(3, 0)], [], 60000), [])

    def test_two_minute_gap_and_first_death(self):
        moments = find_moments([frame(2, 9), frame(3, 9), frame(4, 9), frame(5, 15)], [190000], 60000)
        gap = next(m for m in moments if m['kind'] == 'farm')
        self.assertEqual((gap['start_ms'], gap['end_ms']), (120000, 240000))
        death = next(m for m in moments if m['kind'] == 'death')
        self.assertEqual((death['start_ms'], death['end_ms']), (130000, 190000))

    def test_missing_frames_do_not_become_farm_gaps(self):
        self.assertFalse(find_moments([frame(2, 9), frame(5, 9), frame(6, 9)], [], 60000))

    def test_null_cs_and_missing_opponent_are_not_zero(self):
        self.assertFalse(find_moments([frame(2, None, None), frame(3, None, None), frame(4, None, None)], [], 60000))

    def test_deficit_is_first_observed_threshold(self):
        moments = find_moments([frame(2, 5, -299), frame(3, 10, -300), frame(4, 15, -500)], [], 60000)
        self.assertEqual(len(moments), 1)
        self.assertEqual(moments[0]['end_ms'], 180000)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, self.notes = [Path(self.tmp.name)/name for name in ('matches.db','reviews.db')]
        with closing(sqlite3.connect(self.db)) as c, c:
            c.executescript('''
                CREATE TABLE matches(match_id TEXT, game_start_ms INTEGER, duration_s INTEGER,
                patch TEXT, queue_id INTEGER, my_champion TEXT, my_position TEXT, opp_champion TEXT,
                win INTEGER, my_participant_id INTEGER, opp_participant_id INTEGER);
                CREATE TABLE timelines(match_id TEXT, frame_interval_ms INTEGER);
                CREATE TABLE frames(match_id TEXT, participant_id INTEGER, timestamp_ms INTEGER,
                total_gold INTEGER, xp INTEGER, minions INTEGER, jungle_minions INTEGER);
                CREATE TABLE events(match_id TEXT, type TEXT, victim_id INTEGER, timestamp_ms INTEGER);
                INSERT INTO matches VALUES('test',0,600,'16.18',420,'Ahri','MIDDLE','Zoe',1,1,6);
                INSERT INTO timelines VALUES('test',60000);
                INSERT INTO frames VALUES('test',1,120000,500,100,5,0);
                INSERT INTO frames VALUES('test',6,120000,900,200,9,0);
            ''')
        self.original = self.db.read_bytes()
        self.store = ReviewStore(self.db, self.notes)

    def tearDown(self):
        self.tmp.cleanup()

    def body(self):
        return dict(match_id='test', moment_id='custom-120000', start_ms=120000, end_ms=120000,
                    reason='uncertain', observation='Need to review the wave.', alternative='Check before leaving.', evidence='stats_only')

    def test_persistence_and_match_database_unchanged(self):
        self.store.save_review(self.body())
        self.store.save_focus(dict(goal='Check wave',why_text='A reviewed moment',check_text='One departure'))
        reopened = ReviewStore(self.db,self.notes)
        self.assertEqual(reopened.detail('test')['reviews'][0]['observation'], self.body()['observation'])
        self.assertEqual(reopened.focus()['goal'], 'Check wave')
        self.assertEqual(self.db.read_bytes(),self.original)

    def test_edit_updates_bookmark_without_duplicate(self):
        body=self.body(); self.store.save_review(body); body['observation']='Corrected'; self.store.save_review(body)
        reviews=self.store.detail('test')['reviews']
        self.assertEqual(len(reviews),1)
        self.assertEqual(reviews[0]['observation'],'Corrected')

    def test_invalid_reviews_are_rejected(self):
        for update in ({'start_ms':-1},{'end_ms':700000},{'reason':'made_up'},{'evidence':'confirmed_by_ai'},{'observation':''},{'start_ms':True}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.store.save_review(self.body() | update)
        with self.assertRaises(KeyError):
            self.store.save_review(self.body() | {'match_id':'absent'})

    def test_only_aligned_opponent_samples_are_compared(self):
        self.assertEqual(self.store.detail('test')['frames'][0]['gold_diff'],-400)
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute('UPDATE frames SET timestamp_ms=130000 WHERE participant_id=6')
        self.assertIsNone(self.store.detail('test')['frames'][0]['gold_diff'])

    def test_http_routes_and_origin_boundary(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(self.store))
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(base+'/api/matches') as r:
                self.assertEqual(json.load(r)[0]['match_id'],'test')
            with urlopen(base+'/') as r:
                self.assertTrue(r.headers['Content-Type'].startswith('text/html'))
                self.assertIn(b'<', r.read())
            for path in ['/api/match?id=absent','/.env','/../league.db']:
                with self.assertRaises(HTTPError) as error:
                    urlopen(base+path)
                self.assertEqual(error.exception.code,404)
            req=Request(base+'/api/reviews', data=json.dumps(self.body()).encode(),headers={'Content-Type':'application/json','Origin':'https://example.com'})
            with self.assertRaises(HTTPError) as error:
                urlopen(req)
            self.assertEqual(error.exception.code,403)
            req=Request(base+'/api/reviews', data=json.dumps(self.body()).encode(),headers={'Content-Type':'application/json','Origin':base})
            with urlopen(req) as r:
                self.assertTrue(json.load(r)['saved'])
        finally:
            server.shutdown(); server.server_close(); thread.join()


def schema_db(path, games):
    """A league.db built from fetch_matches.SCHEMA with test_analyze.match() games."""
    with closing(sqlite3.connect(path)) as c:
        c.executescript(SCHEMA)
        for args, kwargs in games:
            store_match(c, riot_match(*args, **kwargs), ME)


def serve(store, rec=None):
    server = ThreadingHTTPServer(('127.0.0.1', 0), make_handler(store, rec))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f'http://127.0.0.1:{server.server_port}'


class StatsApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db, self.notes = [Path(self.tmp.name)/name for name in ('league.db', 'reviews.db')]
        schema_db(self.db, [(("A", "Ahri", True), dict(e_casts=10, immobilizations=5, start=1)),
                            (("B", "Ahri", False), dict(e_casts=20, immobilizations=8, start=2)),
                            (("L", "Lulu", True), dict(start=3))])
        self.original = self.db.read_bytes()
        self.server, self.thread, self.base = serve(ReviewStore(self.db, self.notes))

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.tmp.cleanup()

    def get(self, path):
        with urlopen(self.base + path) as r:
            return json.load(r)

    def test_charm_route(self):
        data = self.get('/api/charm')
        self.assertEqual(set(data), {'games', 'summary', 'by_opponent', 'method'})
        self.assertEqual([g['match_id'] for g in data['games']], ['B', 'A'])
        self.assertEqual(set(data['summary']), {'all', 'wins', 'losses'})
        self.assertEqual(data['summary']['all']['n'], 2)
        self.assertEqual(len(data['summary']['all']['ci']), 2)
        self.assertEqual(self.db.read_bytes(), self.original)

    def test_profile_route(self):
        data = self.get('/api/profile')
        self.assertEqual([c['champion'] for c in data['champions']], ['Ahri', 'Lulu'])
        stat = data['champions'][0]['stats'][0]
        self.assertEqual(set(stat), {'key', 'label', 'value', 'ci', 'unit', 'n'})


class StaticFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.web = Path(self.tmp.name)/'web'
        (self.web/'fonts').mkdir(parents=True)
        (self.web/'index.html').write_text('<p>hi</p>')
        (self.web/'fonts'/'a.woff2').write_bytes(b'wOF2')
        (self.web/'notes.txt').write_text('no')
        (Path(self.tmp.name)/'secret.json').write_text('{}')

    def tearDown(self):
        self.tmp.cleanup()

    def test_serves_known_types_inside_folder_only(self):
        self.assertEqual(static_file('/', self.web)[1], 'text/html')
        self.assertEqual(static_file('/fonts/a.woff2', self.web)[1], 'font/woff2')
        for bad in ['/notes.txt', '/../secret.json', '/fonts/../../secret.json', '/%2e%2e/secret.json',
                    '/fonts\a.woff2', '/.env', '/missing.css', '/fonts']:
            with self.subTest(bad=bad):
                self.assertIsNone(static_file(bad, self.web))


if __name__ == '__main__':
    unittest.main()
