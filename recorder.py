"""OBS auto-record for your own games. Standard library only.

When armed (off by default), a background thread polls Riot's official Live Client
Data API on 127.0.0.1:2999 every POLL_S seconds. It only answers while a game is
running. When it starts answering, OBS is told to StartRecord over obs-websocket v5
and the in-game clock is stored as the video offset; when it stops answering,
StopRecord returns the file path. After the next fetch_matches.py run the recording is
linked to its match_id by start time.

It never shows anything in game, never touches the LCU, and reads no input or memory.
It only talks to 127.0.0.1.
"""
import base64
import hashlib
import json
import os
import socket
import sqlite3
import ssl
import struct
import subprocess
import threading
import time
import urllib.request
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

OBS_EXE = Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe")
OBS_ARGS = ["--minimize-to-tray", "--disable-shutdown-check"]
OBS_HOST, OBS_PORT = "127.0.0.1", 4455
OBS_TIMEOUT_S = 3.0
# Don't launch OBS again this soon after a launch; it takes a few seconds to open.
OBS_LAUNCH_COOLDOWN_S = 60.0

LIVE_URL = "https://127.0.0.1:2999/liveclientdata/gamestats"
LIVE_TIMEOUT_S = 1.0
POLL_S = 2.0
# Consecutive unanswered polls before the game counts as over (one blip is not an end).
MISSES_TO_STOP = 2

# A recording links to the stored match whose gameStartTimestamp is nearest the
# estimated game-clock zero, within this window. gameStartTimestamp is a little
# before clock zero (loading screen), so the window is generous.
LINK_WINDOW_MS = 5 * 60_000

STATUSES = ("recording", "saved", "linked", "failed")

SCHEMA = """
CREATE TABLE IF NOT EXISTS recordings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id     TEXT,
    path         TEXT,
    offset_s     REAL,
    started_at   TEXT,
    status       TEXT NOT NULL,
    game_zero_ms INTEGER,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_recordings_match ON recordings (match_id);
"""

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ObsError(Exception):
    """OBS answered, but refused or failed the request."""


class ObsAuthError(ObsError):
    """Wrong or missing OBS_WS_PASSWORD."""


class WebSocketClosed(ConnectionError):
    def __init__(self, code=None):
        super().__init__(f"WebSocket closed ({code})")
        self.code = code


# ---------------------------------------------------------------- websocket

class WebSocket:
    """Minimal RFC 6455 client: text frames, ping/pong, close. Enough for obs-websocket."""

    def __init__(self, host=OBS_HOST, port=OBS_PORT, timeout=OBS_TIMEOUT_S,
                 protocol="obswebsocket.json", sock=None):
        self.sock = sock or socket.create_connection((host, port), timeout=timeout)
        self.buffer = b""
        self._handshake(f"{host}:{port}", protocol)

    def _handshake(self, host, protocol):
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((
            f"GET / HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Protocol: {protocol}\r\n\r\n"
        ).encode())
        while b"\r\n\r\n" not in self.buffer:
            self._fill()
        head, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if len(lines[0].split()) < 2 or lines[0].split()[1] != "101":
            raise ConnectionError(f"WebSocket upgrade refused: {lines[0]}")
        headers = {k.strip().lower(): v.strip()
                   for k, _, v in (line.partition(":") for line in lines[1:])}
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise ConnectionError("WebSocket upgrade had a bad Sec-WebSocket-Accept")

    def _fill(self):
        chunk = self.sock.recv(65536)
        if not chunk:
            raise WebSocketClosed("eof")
        self.buffer += chunk

    def _read(self, n):
        while len(self.buffer) < n:
            self._fill()
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def _send_frame(self, opcode, payload):
        n = len(payload)
        if n < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
        elif n < 1 << 16:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + mask_bytes(payload, mask))

    def send_text(self, text):
        self._send_frame(0x1, text.encode())

    def recv_text(self):
        parts = []
        while True:
            b1, b2 = self._read(2)
            fin, opcode, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            payload = self._read(n)
            if mask:
                payload = mask_bytes(payload, mask)
            if opcode == 0x8:
                code = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None
                raise WebSocketClosed(code)
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return b"".join(parts).decode()

    def close(self):
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        self.sock.close()


def mask_bytes(payload, mask):
    return bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


# ---------------------------------------------------------------- obs-websocket v5

def obs_auth(password, salt, challenge):
    """obs-websocket v5 auth: base64(sha256(base64(sha256(password + salt)) + challenge))."""
    secret = base64.b64encode(hashlib.sha256((password + salt).encode()).digest())
    return base64.b64encode(hashlib.sha256(secret + challenge.encode()).digest()).decode()


class ObsClient:
    """One obs-websocket v5 session: Hello → Identify → requests. Use as a context manager."""

    def __init__(self, password, connect=WebSocket):
        self.password, self.connect, self.ws = password, connect, None

    def __enter__(self):
        self.ws = self.connect()
        try:
            hello = json.loads(self.ws.recv_text())
            if hello.get("op") != 0:
                raise ObsError("Expected obs-websocket Hello")
            identify = {"rpcVersion": 1, "eventSubscriptions": 0}
            auth = hello.get("d", {}).get("authentication")
            if auth:
                if not self.password:
                    raise ObsAuthError("OBS_WS_PASSWORD is not set in .env")
                identify["authentication"] = obs_auth(self.password, auth["salt"],
                                                      auth["challenge"])
            self.ws.send_text(json.dumps({"op": 1, "d": identify}))
            try:
                reply = json.loads(self.ws.recv_text())
            except WebSocketClosed as exc:
                if exc.code == 4009:
                    raise ObsAuthError("OBS rejected OBS_WS_PASSWORD") from exc
                raise
            if reply.get("op") != 2:
                raise ObsError("OBS did not identify the session")
        except BaseException:
            self.ws.close()
            raise
        return self

    def __exit__(self, *exc):
        self.ws.close()

    def request(self, request_type, data=None):
        request_id = uuid.uuid4().hex
        body = {"requestType": request_type, "requestId": request_id}
        if data:
            body["requestData"] = data
        self.ws.send_text(json.dumps({"op": 6, "d": body}))
        while True:
            msg = json.loads(self.ws.recv_text())
            d = msg.get("d", {})
            if msg.get("op") == 7 and d.get("requestId") == request_id:
                status = d.get("requestStatus", {})
                if not status.get("result"):
                    raise ObsError(f"{request_type} failed: "
                                   f"{status.get('comment') or status.get('code')}")
                return d.get("responseData") or {}


# ---------------------------------------------------------------- live client + OBS process

def live_game_time(url=LIVE_URL, timeout=LIVE_TIMEOUT_S):
    """In-game clock in seconds from the Live Client Data API, or None when not in game.

    The API uses a self-signed certificate, so verification is off; that is only
    acceptable because the URL is pinned to 127.0.0.1.
    """
    if not url.startswith("https://127.0.0.1:"):
        raise ValueError("The Live Client Data API is only polled on 127.0.0.1")
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(url, context=context, timeout=timeout) as r:
            value = json.load(r).get("gameTime")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def port_open(host=OBS_HOST, port=OBS_PORT, timeout=0.3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def launch_obs(exe=OBS_EXE):
    """Start OBS minimised to the tray. OBS must run from its own folder to find its data."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(exe), *OBS_ARGS], cwd=str(Path(exe).parent), creationflags=flags,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, close_fds=True)


# ---------------------------------------------------------------- storage

def ensure_schema(conn):
    conn.executescript(SCHEMA)


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def last_recording(conn):
    row = conn.execute("SELECT match_id, path, status, started_at FROM recordings "
                       "ORDER BY id DESC LIMIT 1").fetchone()
    return None if row is None else dict(zip(("match_id", "path", "status", "started_at"), row))


def recording_for(conn, match_id):
    """Newest linked recording row for a match: (path, offset_s, status) or None."""
    return conn.execute("SELECT path, offset_s, status FROM recordings WHERE match_id = ? "
                        "AND path IS NOT NULL ORDER BY id DESC LIMIT 1", (match_id,)).fetchone()


def link_recordings(notes_conn, league_conn, window_ms=LINK_WINDOW_MS):
    """Link saved recordings to the stored match that started nearest to them.

    The match is the one whose gameStartTimestamp is nearest the recording's
    estimated game-clock zero, within `window_ms`, and not already linked.
    Returns the number linked. A wrong link is possible if two games started
    within the window (e.g. a remake); the manual offset stays as the fallback.
    """
    linked = {r[0] for r in notes_conn.execute(
        "SELECT match_id FROM recordings WHERE match_id IS NOT NULL")}
    count = 0
    for rec_id, zero in notes_conn.execute(
            "SELECT id, game_zero_ms FROM recordings WHERE status = 'saved' "
            "AND match_id IS NULL AND game_zero_ms IS NOT NULL ORDER BY id").fetchall():
        candidates = [(abs(start - zero), mid) for mid, start in league_conn.execute(
            "SELECT match_id, game_start_ms FROM matches WHERE game_start_ms BETWEEN ? AND ?",
            (zero - window_ms, zero + window_ms)) if mid not in linked]
        if not candidates:
            continue
        match_id = min(candidates)[1]
        notes_conn.execute("UPDATE recordings SET match_id = ?, status = 'linked' WHERE id = ?",
                           (match_id, rec_id))
        linked.add(match_id)
        count += 1
    notes_conn.commit()
    return count


# ---------------------------------------------------------------- recorder

class Recorder:
    """Arms/disarms auto-record and runs the poll loop. Every side effect is injectable.

    `step()` is one poll; the thread just calls it every `poll_s`. Tests drive
    `step()` directly with a fake probe and a fake OBS.
    """

    def __init__(self, notes_path, password=None, probe=live_game_time, obs_factory=None,
                 launcher=launch_obs, obs_exe=OBS_EXE, obs_reachable=port_open,
                 clock=time.time, poll_s=POLL_S):
        self.notes_path = Path(notes_path)
        self._password = password
        self.probe, self.launcher, self.obs_exe = probe, launcher, Path(obs_exe)
        self.obs_reachable, self.clock, self.poll_s = obs_reachable, clock, poll_s
        self.obs_factory = obs_factory or (lambda pw: ObsClient(pw))
        self.armed = False
        self.in_game = False
        self.recording_id = None
        self.misses = 0
        self.game_failed = False
        self.auth_failed = False
        self.error = None
        self.launched_at = None
        self.lock = threading.RLock()
        self.thread = None
        self.stop_event = threading.Event()
        with closing(self._db()) as conn, conn:
            ensure_schema(conn)

    @property
    def password(self):
        return self._password if self._password is not None else os.environ.get("OBS_WS_PASSWORD")

    def _db(self):
        return sqlite3.connect(self.notes_path, timeout=5)

    # ---- state for the API

    def obs_state(self):
        """running | stopped | unavailable. A quick local port check; starts nothing."""
        if not self.password or self.auth_failed:
            return "unavailable"
        if self.obs_reachable():
            return "running"
        return "stopped" if self.obs_exe.exists() else "unavailable"

    def status(self):
        """The /api/recorder payload. Lock-free so a slow poll never blocks the page."""
        with closing(self._db()) as conn:
            last = last_recording(conn)
        out = {"armed": self.armed, "obs": self.obs_state(),
               "game": "in_game" if self.in_game else "idle", "last": last}
        if not self.password:
            out["error"] = "OBS_WS_PASSWORD is not set in .env"
        elif self.error:
            out["error"] = self.error
        return out

    def set_armed(self, armed):
        with self.lock:
            self.armed = bool(armed)
            if self.armed:
                self.error = None
                self.auth_failed = False
                self.ensure_obs()
                if self.thread is None:
                    self.thread = threading.Thread(target=self._loop, name="recorder",
                                                   daemon=True)
                    self.thread.start()
            elif self.recording_id is None:
                self.in_game = False
                self.stop_event.set()
        return self.status()

    def _loop(self):
        """Poll until disarmed with no recording in progress. stop_event only wakes it early."""
        while True:
            with self.lock:
                try:
                    keep = self.step()
                except Exception as exc:  # keep polling; show the problem in the status
                    keep, self.error = True, f"Recorder error: {exc}"
                if not keep:
                    self.thread = None
                    return
            self.stop_event.wait(self.poll_s)
            self.stop_event.clear()

    # ---- OBS

    def ensure_obs(self):
        """Launch OBS if it isn't reachable, at most once per cooldown."""
        if self.obs_reachable() or not self.obs_exe.exists():
            return
        now = self.clock()
        if self.launched_at is not None and now - self.launched_at < OBS_LAUNCH_COOLDOWN_S:
            return
        try:
            self.launcher(self.obs_exe)
            self.launched_at = now
        except OSError as exc:
            self.error = f"Could not start OBS: {exc}"

    def _obs(self, request_type):
        if not self.password:
            raise ObsAuthError("OBS_WS_PASSWORD is not set in .env")
        with self.obs_factory(self.password) as obs:
            return obs.request(request_type)

    # ---- one poll

    def step(self):
        """One poll. Returns False when the loop should exit (disarmed and not recording)."""
        with self.lock:
            if not self.armed and self.recording_id is None:
                self.in_game = False
                return False
            game_time = self.probe()
            if game_time is not None:
                self.misses = 0
                self.in_game = True
                if self.recording_id is None and not self.game_failed and self.armed:
                    self._start(game_time)
            else:
                self.misses += 1
                if self.misses >= MISSES_TO_STOP:
                    if self.recording_id is not None:
                        self._stop()
                    self.in_game = False
                    self.game_failed = False
            return self.armed or self.recording_id is not None

    def _start(self, game_time):
        before = self.clock()
        try:
            self._obs("StartRecord")
        except ObsAuthError as exc:
            self.auth_failed, self.error = True, str(exc)
            self._failed_row(str(exc))
            return
        except ObsError as exc:
            self.error = str(exc)
            self._failed_row(str(exc))
            return
        except OSError:
            # OBS not up yet: launch it and try again on the next poll.
            self.error = "OBS is not reachable on port 4455; starting it"
            self.ensure_obs()
            return
        started = self.clock()
        now_game = self.probe()
        if now_game is None:
            now_game = game_time + (started - before)
        self.error = None
        with closing(self._db()) as conn, conn:
            cur = conn.execute(
                "INSERT INTO recordings (offset_s, started_at, status, game_zero_ms) "
                "VALUES (?,?,?,?)",
                (round(-now_game, 1), iso(started), "recording",
                 int(round((started - now_game) * 1000))))
            self.recording_id = cur.lastrowid

    def _failed_row(self, message):
        self.game_failed = True
        with closing(self._db()) as conn, conn:
            conn.execute("INSERT INTO recordings (started_at, status, error) VALUES (?,?,?)",
                         (iso(self.clock()), "failed", message))

    def _stop(self):
        rec_id, self.recording_id = self.recording_id, None
        try:
            path = self._obs("StopRecord").get("outputPath")
            status, error = ("saved", None) if path else ("failed", "OBS returned no file path")
        except (ObsError, OSError) as exc:
            path, status, error = None, "failed", f"StopRecord failed: {exc}"
        self.error = error
        with closing(self._db()) as conn, conn:
            conn.execute("UPDATE recordings SET path = ?, status = ?, error = ? WHERE id = ?",
                         (path, status, error, rec_id))
