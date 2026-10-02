"""Local, dependency-free post-game review. Run: python review_app.py"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import recorder
import review_data
from fetch_matches import load_dotenv
from review_data import now

ROOT = Path(__file__).resolve().parent
WEB = ROOT / 'review_web'
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


class ReviewStore(review_data.ReviewStore):
    """review_data.ReviewStore plus the URL this server streams the recording from."""

    def recording(self, match_id):
        """{path, offset_s, status, url} for the match's recording, or None.

        url is set only when the linked file still exists; see recording_path.
        """
        out = super().recording(match_id)
        if out is not None and self.recording_path(match_id):
            out['url'] = '/api/recording-file?id=' + quote(match_id)
        return out


VIDEO_TYPES = {'.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.webm': 'video/webm',
               '.mov': 'video/quicktime'}
CHUNK = 1 << 20
# Recent Ahri/Zoe games fetch_matches.py checks per refresh. Stored games cost no match calls.
FETCH_COUNT = 20
FETCH_PROGRESS = re.compile(r'\[(\d+)/(\d+)\]')


class Fetcher:
    """Runs fetch_matches.py in a child process, one run at a time.

    A child process keeps this app standard-library only, and a rejected key
    (fetch_matches exits) cannot stop the server. `added` counts reviewable games
    (Ahri/Zoe mid with a timeline) gained by the run, not every match stored.
    """

    def __init__(self, db_path, count_games, command=None):
        self.count_games = count_games
        self.command = command or [sys.executable, str(ROOT/'fetch_matches.py'),
                                   '--count', str(FETCH_COUNT), '--db', str(Path(db_path).resolve())]
        self.lock = threading.Lock()
        self.state = dict(running=False, started_at=None, finished_at=None, ok=None,
                          message='', progress=None, added=None)

    def status(self):
        with self.lock:
            return dict(self.state)

    def start(self):
        with self.lock:
            if self.state['running']:
                return dict(self.state)
            self.state = dict(running=True, started_at=now(), finished_at=None, ok=None,
                              message='Starting fetch_matches.py', progress=None, added=None)
        threading.Thread(target=self._run, args=(self._count(),), daemon=True).start()
        return self.status()

    def _count(self):
        try:
            return self.count_games()
        except sqlite3.Error:
            return None

    def _run(self, before):
        lines = []
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        try:
            proc = subprocess.Popen(self.command, cwd=ROOT, env=env, text=True, encoding='utf-8',
                                    errors='replace', stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            for line in proc.stdout:
                if not line.strip():
                    continue
                lines.append(line.strip())
                if m := FETCH_PROGRESS.search(line):
                    with self.lock:
                        self.state.update(progress=[int(m[1]), int(m[2])],
                                          message=f'Checked {m[1]} of {m[2]} games')
            code = proc.wait()
        except OSError as exc:
            code, lines = -1, [f'Could not start fetch_matches.py: {exc}']
        after = self._count()
        added = after - before if before is not None and after is not None else None
        with self.lock:
            self.state.update(running=False, finished_at=now(), ok=code == 0, added=added,
                              message=fetch_message(code, lines, added))


def fetch_message(code, lines, added):
    """One line for the status bar: what the fetch did, or why it stopped."""
    if code == 0:
        if not added:
            return 'No new games.' if added == 0 else 'Fetch finished.'
        return f'Added {added} new game{"s" * (added != 1)}.'
    last = lines[-1] if lines else f'fetch_matches.py exited with code {code}.'
    if "No module named 'requests'" in last:
        return 'fetch_matches.py needs requests: run py -m pip install requests, then retry.'
    return last[:300]



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


def make_handler(store, rec=None, fetcher=None):
    rec = rec or recorder.Recorder(store.notes_path)
    fetcher = fetcher or Fetcher(store.matches_path, lambda: len(store.matches()))

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
                elif url.path == '/api/fetch':
                    self.respond(fetcher.status())
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
                elif self.path == '/api/fetch':
                    result = fetcher.start()
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
