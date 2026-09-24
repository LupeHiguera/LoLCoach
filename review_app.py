"""Local, dependency-free post-game review. Run: python review_app.py"""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
REASONS = {"uncertain", "dead", "recalling", "fighting", "roaming", "pressured", "other"}


def connect(path, readonly=False):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + ("?mode=ro" if readonly else "?mode=rwc"), uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class ReviewStore:
    def __init__(self, matches_path, notes_path):
        self.matches_path, self.notes_path = matches_path, notes_path
        if Path(matches_path).resolve() == Path(notes_path).resolve():
            raise ValueError("Match and review databases must be different files.")
        with closing(connect(matches_path, True)) as conn:
            conn.execute("SELECT match_id FROM matches LIMIT 1")
        with closing(connect(notes_path)) as conn, conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS reviews (
                    match_id TEXT, moment_id TEXT, start_ms INTEGER, end_ms INTEGER,
                    reason TEXT, observation TEXT, alternative TEXT, evidence TEXT,
                    updated_at TEXT, PRIMARY KEY(match_id, moment_id));
                CREATE TABLE IF NOT EXISTS focus (
                    id INTEGER PRIMARY KEY CHECK(id=1), goal TEXT, why_text TEXT,
                    check_text TEXT, updated_at TEXT);
            """)

    def matches(self):
        with closing(connect(self.matches_path, True)) as conn:
            return [dict(r) for r in conn.execute("""
                SELECT m.match_id, game_start_ms, duration_s, patch, queue_id,
                       my_champion, my_position, opp_champion, win
                FROM matches m JOIN timelines t USING(match_id)
                WHERE my_champion IN ('Ahri','Zoe') AND my_position='MIDDLE'
                ORDER BY game_start_ms DESC
            """)]

    def detail(self, match_id):
        with closing(connect(self.matches_path, True)) as conn:
            row = conn.execute("""SELECT match_id, game_start_ms, duration_s, patch, queue_id,
                my_champion, my_position, opp_champion, win, my_participant_id, opp_participant_id
                FROM matches WHERE match_id=?""", (match_id,)).fetchone()
            if row is None:
                raise KeyError("Match not found")
            match = dict(row)
            interval = conn.execute("SELECT frame_interval_ms FROM timelines WHERE match_id=?", (match_id,)).fetchone()
            cadence = (interval[0] if interval else None) or 60000
            frames = [dict(r) for r in conn.execute("""
                SELECT a.timestamp_ms AS time, a.total_gold AS gold, a.xp, a.minions AS cs,
                    a.jungle_minions AS jungle_cs,
                    a.total_gold-b.total_gold AS gold_diff, a.xp-b.xp AS xp_diff,
                    a.minions-b.minions AS cs_diff
                FROM frames a LEFT JOIN frames b ON a.match_id=b.match_id
                    AND b.participant_id=? AND a.timestamp_ms=b.timestamp_ms
                WHERE a.match_id=? AND a.participant_id=? ORDER BY a.timestamp_ms
            """, (match['opp_participant_id'], match_id, match['my_participant_id']))]
            deaths = [r[0] for r in conn.execute("""SELECT timestamp_ms FROM events
                WHERE match_id=? AND type='CHAMPION_KILL' AND victim_id=? ORDER BY timestamp_ms""",
                (match_id, match['my_participant_id']))]
        moments = find_moments(frames, deaths, cadence)
        with closing(connect(self.notes_path)) as conn:
            reviews = [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE match_id=? ORDER BY start_ms", (match_id,))]
        return dict(match=match, frames=frames, deaths=deaths, moments=moments, reviews=reviews, cadence_ms=cadence)

    def focus(self):
        with closing(connect(self.notes_path)) as conn:
            r = conn.execute("SELECT goal, why_text, check_text FROM focus WHERE id=1").fetchone()
            return dict(r) if r else dict(goal='', why_text='', check_text='')

    def save_focus(self, body):
        values = [text_field(body, key, 1000) for key in ('goal', 'why_text', 'check_text')]
        if not values[0].strip():
            raise ValueError("Write a practice goal first.")
        with closing(connect(self.notes_path)) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO focus VALUES (1,?,?,?,?)", (*values, now()))
        return self.focus()

    def save_review(self, body):
        match_id = text_field(body, 'match_id', 100)
        detail = self.detail(match_id)
        start, end = body.get('start_ms'), body.get('end_ms')
        duration = max([detail['match']['duration_s'] * 1000] + [f['time'] for f in detail['frames']])
        if type(start) is not int or type(end) is not int or not 0 <= start <= end <= duration:
            raise ValueError("Choose a timestamp within this match.")
        moment_id = text_field(body, 'moment_id', 100)
        if not moment_id:
            raise ValueError("Choose a moment first.")
        reason = text_field(body, 'reason', 30)
        if reason not in REASONS:
            raise ValueError("Choose a valid reason.")
        observation = text_field(body, 'observation', 3000)
        alternative = text_field(body, 'alternative', 3000)
        evidence = text_field(body, 'evidence', 30)
        if evidence not in {'stats_only', 'recording', 'memory'}:
            raise ValueError("Choose the evidence you reviewed.")
        if not observation.strip():
            raise ValueError("Add an observation before saving.")
        with closing(connect(self.notes_path)) as conn, conn:
            conn.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?,?)",
                         (match_id, moment_id, start, end, reason, observation, alternative, evidence, now()))
        return dict(saved=True)


def text_field(body, key, maximum):
    value = body.get(key, '')
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError(f"Invalid or too long: {key}")
    return value.strip()


def now():
    return datetime.now(timezone.utc).isoformat()


def find_moments(frames, deaths, cadence):
    """Review prompts, not diagnoses. Never infer movement or missed CS."""
    moments = []
    for i, ts in enumerate(deaths):
        moments.append(dict(id=f'death-{ts}', start_ms=max(0, ts-60000), end_ms=ts,
                            title='First death' if i == 0 else f'Death {i+1}', kind='death',
                            description='Review the minute before this death. What information and options did you have?'))
    deficit = next((f for f in frames if f['time'] >= 120000 and f['gold_diff'] is not None and f['gold_diff'] <= -300), None)
    if deficit:
        ts = deficit['time']
        moments.append(dict(id=f'deficit-{ts}', start_ms=max(0, ts-cadence), end_ms=ts,
                            title='First sampled gold deficit of 300+', kind='deficit',
                            description=f"{abs(deficit['gold_diff'])} gold behind the assigned lane opponent at this snapshot. This threshold is a review prompt, not a mistake score."))
    run = None
    gaps = []
    for a, b in zip(frames, frames[1:]):
        dt = b['time'] - a['time']
        valid = (a['time'] >= 120000 and 0 < dt <= cadence*1.25
                 and a['cs'] is not None and b['cs'] is not None and b['cs'] == a['cs'])
        if valid:
            if run is None:
                run = [a['time'], b['time']]
            else:
                run[1] = b['time']
        elif run is not None:
            gaps.append(run)
            run = None
    if run is not None:
        gaps.append(run)
    for start, end in gaps:
        if end-start < 120000:
            continue
        moments.append(dict(id=f'farm-{start}-{end}', start_ms=start, end_ms=end,
                            title='No lane CS gained', kind='farm',
                            description='No increase in lane-minion kills between these snapshots for at least two minutes. Jungle CS is separate. Review the cause; this does not prove missed farm.'))
    return sorted(moments, key=lambda m: (m['end_ms'], m['kind']))


def make_handler(store):
    class Handler(BaseHTTPRequestHandler):
        def respond(self, data, status=200, mime='application/json'):
            payload = json.dumps(data).encode() if mime == 'application/json' else data
            self.send_response(status)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def allowed(self, post=False):
            host = self.headers.get('Host', '')
            valid = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
            if host not in valid:
                self.respond({'error': 'Local access only'}, 403)
                return False
            origin = self.headers.get('Origin')
            if post and origin is not None and origin != 'http://' + host:
                self.respond({'error': 'Origin rejected'}, 403)
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            url = urlsplit(self.path)
            try:
                if url.path == '/api/matches':
                    self.respond(store.matches())
                elif url.path == '/api/match':
                    self.respond(store.detail(parse_qs(url.query).get('id', [''])[0]))
                elif url.path == '/api/focus':
                    self.respond(store.focus())
                elif url.path in {'/', '/app.js', '/style.css'}:
                    filename, mime = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'application/javascript'), '/style.css': ('style.css', 'text/css')}[url.path]
                    self.respond((ROOT/'review_web'/filename).read_bytes(), mime=mime)
                else:
                    self.respond({'error': 'Not found'}, 404)
            except KeyError:
                self.respond({'error': 'Match not found'}, 404)
            except sqlite3.Error:
                self.respond({'error': 'Unable to read the database. Retry after the import finishes.'}, 503)

        def do_POST(self):
            if not self.allowed(post=True):
                return
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                self.respond({'error': 'JSON required'}, 415)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 20000:
                    raise ValueError('Invalid request size')
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError('Expected an object')
                if self.path == '/api/reviews':
                    result = store.save_review(body)
                elif self.path == '/api/focus':
                    result = store.save_focus(body)
                else:
                    self.respond({'error': 'Not found'}, 404)
                    return
                self.respond(result)
            except (ValueError, KeyError, TypeError) as exc:
                self.respond({'error': str(exc)}, 400)
            except sqlite3.Error:
                self.respond({'error': 'Could not save. Your text is still here; please retry.'}, 503)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT/'league.db')
    parser.add_argument('--notes', type=Path, default=ROOT/'reviews.db')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    try:
        store = ReviewStore(args.db, args.notes)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store))
    except (sqlite3.Error, OSError, ValueError) as exc:
        parser.exit(1, f'Cannot start review app: {exc}\nImport matches first, and check the database path and port.\n')
    print(f'Review app: http://127.0.0.1:{args.port}  (Ctrl+C to stop)', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
