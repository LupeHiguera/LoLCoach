"""Local, dependency-free post-game review. Run: python review_app.py"""
import argparse
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import analyze
import recorder
from fetch_matches import load_dotenv

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'review_web'
REASONS = {"uncertain", "dead", "recalling", "fighting", "roaming", "pressured", "other"}
# Static files the app will serve from review_web/, by extension.
STATIC_TYPES = {
    '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css',
    '.json': 'application/json', '.woff2': 'font/woff2', '.woff': 'font/woff',
    '.ttf': 'font/ttf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.svg': 'image/svg+xml', '.ico': 'image/x-icon',
}
TEXT_TYPES = {'text/html', 'application/javascript', 'text/css', 'application/json',
              'image/svg+xml'}


def static_file(url_path, web=WEB):
    """(path, mime) for a file inside review_web/, or None. Never escapes the folder."""
    rel = unquote(url_path).lstrip('/') or 'index.html'
    if '\\' in rel or '\0' in rel or any(part in ('', '.', '..') or part.startswith('.')
                                         for part in rel.split('/')):
        return None
    mime = STATIC_TYPES.get(Path(rel).suffix.lower())
    base = Path(web).resolve()
    path = (base / rel).resolve()
    if mime is None or base not in path.parents or not path.is_file():
        return None
    return path, mime


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
            recorder.ensure_schema(conn)

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
        return dict(match=match, frames=frames, deaths=deaths, moments=moments, reviews=reviews,
                    cadence_ms=cadence, recording=self.recording(match_id))

    def link_recordings(self):
        """Link saved OBS recordings to matches fetched since (see recorder.link_recordings)."""
        with closing(connect(self.notes_path)) as notes, closing(connect(self.matches_path, True)) as league:
            return recorder.link_recordings(notes, league)

    def recording_path(self, match_id):
        """The linked recording's file if it still exists. Only table rows are ever served."""
        with closing(connect(self.notes_path)) as conn:
            row = recorder.recording_for(conn, match_id)
        path = Path(row[0]) if row else None
        return path if path is not None and path.is_file() else None

    def recording(self, match_id):
        """{path, offset_s, status, url} for the match's recording, or None.

        offset_s is added to game time to get video time. It comes from the game
        clock read when OBS started, so it can be off by a second or so.
        """
        self.link_recordings()
        with closing(connect(self.notes_path)) as conn:
            row = recorder.recording_for(conn, match_id)
        if row is None:
            return None
        url = ('/api/recording-file?id=' + quote(match_id)) if self.recording_path(match_id) else None
        return dict(path=row['path'], offset_s=row['offset_s'], status=row['status'], url=url)

    def charm(self):
        """Ahri Charm estimate per game and pooled with bootstrap CIs (analyze.charm_report)."""
        with closing(connect(self.matches_path, True)) as conn:
            return analyze.charm_report(conn)

    def profile(self):
        """Per-champion end-of-game habits with bootstrap CIs (analyze.profile_report)."""
        with closing(connect(self.matches_path, True)) as conn:
            return analyze.profile_report(conn)

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


VIDEO_TYPES = {'.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.webm': 'video/webm',
               '.mov': 'video/quicktime'}
CHUNK = 1 << 20


def byte_range(header, size):
    """(start, end) inclusive for a single `bytes=` Range header, None for whole file,
    or ValueError when it can't be satisfied."""
    if not header:
        return None
    unit, _, spec = header.partition('=')
    if unit.strip() != 'bytes' or ',' in spec:
        raise ValueError('Only single byte ranges are supported')
    first, _, last = spec.strip().partition('-')
    if first:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    else:
        start, end = max(0, size - int(last)), size - 1
    if start > end or start >= size:
        raise ValueError('Range not satisfiable')
    return start, end


def make_handler(store, rec=None):
    rec = rec or recorder.Recorder(store.notes_path)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, data, status=200, mime='application/json'):
            payload = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(status)
            self.send_header('Content-Type', mime + ('; charset=utf-8' if mime in TEXT_TYPES else ''))
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(payload)

        def send_video(self, path):
            size = path.stat().st_size
            try:
                span = byte_range(self.headers.get('Range'), size)
            except ValueError:
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            start, end = span or (0, size - 1)
            self.send_response(206 if span else 200)
            self.send_header('Content-Type', VIDEO_TYPES.get(path.suffix.lower(), 'application/octet-stream'))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Length', str(end - start + 1))
            if span:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            with open(path, 'rb') as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(CHUNK, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)

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
                elif url.path == '/api/charm':
                    self.respond(store.charm())
                elif url.path == '/api/profile':
                    self.respond(store.profile())
                elif url.path == '/api/recorder':
                    store.link_recordings()
                    self.respond(rec.status())
                elif url.path == '/api/recording-file':
                    path = store.recording_path(parse_qs(url.query).get('id', [''])[0])
                    if path is None:
                        self.respond({'error': 'No recording for this match'}, 404)
                    else:
                        self.send_video(path)
                elif not url.path.startswith('/api/') and (found := static_file(url.path)):
                    path, mime = found
                    self.respond(path.read_bytes(), mime=mime)
                else:
                    self.respond({'error': 'Not found'}, 404)
            except KeyError:
                self.respond({'error': 'Match not found'}, 404)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the video element dropped a range request; nothing to answer
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
                elif self.path == '/api/recorder':
                    if type(body.get('armed')) is not bool:
                        raise ValueError('armed must be true or false')
                    result = rec.set_armed(body['armed'])
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
        load_dotenv(ROOT/'.env')
        store = ReviewStore(args.db, args.notes)
        server = ThreadingHTTPServer(('127.0.0.1', args.port), make_handler(store, recorder.Recorder(args.notes)))
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
