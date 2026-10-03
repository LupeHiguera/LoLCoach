"""Recorder tests. OBS and the Live Client Data API are always faked: no network, no OBS."""
import base64
import hashlib
import json
import socket
import sqlite3
import struct
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

import recorder
from fetch_matches import SCHEMA, store_match
from recorder import (MISSES_TO_STOP, ObsAuthError, ObsClient, ObsError, Recorder, WebSocket,
                      WebSocketClosed, link_recordings, live_game_time, obs_auth)
from review_data import ReviewStore
from test_analyze import ME, match as riot_match


# ---------------------------------------------------------------- fakes

class FakeWS:
    """Scripted obs-websocket peer: `incoming` is what OBS sends, `sent` what we sent."""

    def __init__(self, incoming):
        self.incoming, self.sent, self.closed = list(incoming), [], False

    def send_text(self, text):
        self.sent.append(json.loads(text))

    def recv_text(self):
        item = self.incoming.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            item = item(self.sent[-1])
        return json.dumps(item)

    def close(self):
        self.closed = True


def reply(ok=True, data=None, comment=None):
    """A RequestResponse for whatever request was sent last."""
    def build(sent):
        d = sent["d"]
        return {"op": 7, "d": {"requestType": d["requestType"], "requestId": d["requestId"],
                               "requestStatus": {"result": ok, "code": 100 if ok else 500,
                                                 "comment": comment},
                               "responseData": data}}
    return build


HELLO_AUTH = {"op": 0, "d": {"obsWebSocketVersion": "5.6.3", "rpcVersion": 1,
                             "authentication": {"challenge": "c+h", "salt": "s/a"}}}
IDENTIFIED = {"op": 2, "d": {"negotiatedRpcVersion": 1}}


class FakeObs:
    """Stands in for ObsClient in Recorder tests."""

    def __init__(self):
        self.requests, self.fail, self.unreachable, self.recording = [], None, False, False
        self.output_path, self.lookup_reply = None, None
        self.stop_path = r"C:\Videos\game.mkv"

    def __call__(self, password):
        self.password = password
        return self

    def __enter__(self):
        if self.unreachable:
            raise ConnectionRefusedError("no OBS")
        return self

    def __exit__(self, *exc):
        pass

    def request(self, request_type, data=None):
        if request_type == 'GetRecordStatus':
            return {'outputActive': self.recording}
        if request_type == 'GetOutputList':
            return {'outputs': [{'outputName': 'simple_file_output', 'outputActive': self.recording}]}
        if request_type == 'GetOutputSettings':
            if isinstance(self.lookup_reply, Exception):
                raise self.lookup_reply
            return self.lookup_reply or {'outputSettings': {'path': self.output_path}}
        self.requests.append(request_type)
        if self.fail:
            raise self.fail
        if request_type == "StartRecord":
            self.recording = True
            return {}
        if request_type == "StopRecord":
            self.recording = False
            return {"outputPath": self.stop_path}
        return {}


class Probe:
    """Live Client gameTime sequence; None means the API did not answer."""

    def __init__(self, *values):
        self.values, self.calls = list(values), 0

    def __call__(self):
        self.calls += 1
        return self.values.pop(0) if self.values else None


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------- protocol

class AuthTests(unittest.TestCase):
    def test_obs_v5_auth_formula(self):
        secret = base64.b64encode(hashlib.sha256(b"pw" + b"salt").digest())
        expected = base64.b64encode(hashlib.sha256(secret + b"challenge").digest()).decode()
        self.assertEqual(obs_auth("pw", "salt", "challenge"), expected)
        self.assertNotEqual(obs_auth("pw", "salt", "challenge"), obs_auth("pw2", "salt", "challenge"))


class RecordingStartTests(unittest.TestCase):
    def test_acknowledged_start_waits_for_active_output(self):
        from unittest import mock
        obs = mock.Mock()
        obs.request.side_effect = [{'outputActive': False}, {'outputActive': True}]
        clock = mock.Mock(side_effect=[0, 0.1])
        sleep = mock.Mock()
        recorder.wait_for_recording(obs, clock=clock, sleep=sleep)
        self.assertEqual(obs.request.call_count, 2)
        sleep.assert_called_once_with(recorder.OBS_START_POLL_S)

    def test_inactive_output_times_out_without_a_sync_offset(self):
        from unittest import mock
        obs = mock.Mock()
        obs.request.return_value = {'outputActive': False}
        with self.assertRaisesRegex(ObsError, 'did not become active'):
            recorder.wait_for_recording(obs, timeout_s=1, clock=mock.Mock(side_effect=[0, 2]),
                                        sleep=mock.Mock())


class WebSocketTests(unittest.TestCase):
    """Real frames over a socketpair against a tiny hand-written server."""

    def setUp(self):
        self.client, self.server = socket.socketpair()
        self.server.settimeout(5)
        self.client.settimeout(5)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.received = []

    def tearDown(self):
        self.client.close()
        self.server.close()

    def read_exact(self, n):
        data = b""
        while len(data) < n:
            data += self.server.recv(n - len(data))
        return data

    def read_frame(self):
        b1, b2 = self.read_exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self.read_exact(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self.read_exact(8))[0]
        self.assertTrue(b2 & 0x80, "client frames must be masked")
        mask = self.read_exact(4)
        return b1 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(self.read_exact(n)))

    def frame(self, opcode, payload, fin=True):
        n = len(payload)
        head = bytes([(0x80 if fin else 0) | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 1 << 16:
            head += bytes([126]) + struct.pack("!H", n)
        else:
            head += bytes([127]) + struct.pack("!Q", n)
        return head + payload

    def serve(self):
        request = b""
        while b"\r\n\r\n" not in request:
            request += self.server.recv(4096)
        key = [line.split(b":", 1)[1].strip() for line in request.split(b"\r\n")
               if line.lower().startswith(b"sec-websocket-key")][0]
        accept = base64.b64encode(hashlib.sha1(key + recorder.WS_GUID.encode()).digest())
        # Send the 101 and a ping plus a fragmented message in the same packet.
        self.server.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                            b"Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n"
                            + self.frame(0x9, b"hi") + self.frame(0x1, b"hel", fin=False)
                            + self.frame(0x0, b"lo"))
        self.received.append(self.read_frame())  # the pong
        for _ in range(3):
            opcode, payload = self.read_frame()
            self.received.append((opcode, payload))
            self.server.sendall(self.frame(0x1, payload))
        self.server.sendall(self.frame(0x8, struct.pack("!H", 4009)))

    def test_handshake_frames_ping_and_close(self):
        self.thread.start()
        ws = WebSocket(host="127.0.0.1", port=4455, sock=self.client)
        self.assertEqual(ws.recv_text(), "hello")
        for size in (5, 300, 70_000):
            text = "x" * size
            ws.send_text(text)
            self.assertEqual(ws.recv_text(), text)
        with self.assertRaises(WebSocketClosed) as closed:
            ws.recv_text()
        self.assertEqual(closed.exception.code, 4009)
        self.thread.join(5)
        self.assertEqual(self.received[0], (0xA, b"hi"))
        self.assertEqual([len(p) for _, p in self.received[1:]], [5, 300, 70_000])


class ObsClientTests(unittest.TestCase):
    def test_identify_with_auth_then_request_skips_events(self):
        ws = FakeWS([HELLO_AUTH, IDENTIFIED, {"op": 5, "d": {"eventType": "RecordStateChanged"}},
                     reply(data={"outputPath": "C:/v.mkv"})])
        with ObsClient("pw", connect=lambda: ws) as obs:
            self.assertEqual(obs.request("StopRecord"), {"outputPath": "C:/v.mkv"})
        identify = ws.sent[0]
        self.assertEqual(identify["op"], 1)
        self.assertEqual(identify["d"]["authentication"], obs_auth("pw", "s/a", "c+h"))
        self.assertEqual(ws.sent[1]["d"]["requestType"], "StopRecord")
        self.assertTrue(ws.closed)

    def test_no_auth_required(self):
        ws = FakeWS([{"op": 0, "d": {"rpcVersion": 1}}, IDENTIFIED, reply()])
        with ObsClient(None, connect=lambda: ws) as obs:
            self.assertEqual(obs.request("StartRecord"), {})
        self.assertNotIn("authentication", ws.sent[0]["d"])

    def test_wrong_password_and_missing_password(self):
        ws = FakeWS([HELLO_AUTH, WebSocketClosed(4009)])
        with self.assertRaises(ObsAuthError):
            with ObsClient("bad", connect=lambda: ws):
                pass
        self.assertTrue(ws.closed)
        with self.assertRaises(ObsAuthError):
            with ObsClient("", connect=lambda: FakeWS([HELLO_AUTH])):
                pass

    def test_failed_request_raises(self):
        ws = FakeWS([HELLO_AUTH, IDENTIFIED, reply(ok=False, comment="OutputRunning")])
        with ObsClient("pw", connect=lambda: ws) as obs, self.assertRaises(ObsError) as err:
            obs.request("StartRecord")
        self.assertIn("OutputRunning", str(err.exception))
        self.assertEqual(err.exception.code, 500)


class OutputPathTests(unittest.TestCase):
    def lookup(self, outputs, path):
        replies = {'GetOutputList': {'outputs': outputs},
                   'GetOutputSettings': {'outputSettings': {'path': path}}}

        class Obs:
            def request(self, request_type, data=None):
                return replies[request_type]
        return recorder.active_recording_path(Obs())

    def test_only_an_active_local_video_file_is_pinned(self):
        local = str(Path(tempfile.gettempdir()) / 'game.mkv')
        active = [{'outputName': 'adv_file_output', 'outputActive': True}]
        self.assertEqual(self.lookup(active, local), local)
        for outputs, path in (
                ([{'outputName': 'simple_file_output', 'outputActive': False}], local),
                ([{'outputName': 'Replay Buffer', 'outputActive': True}], local),
                (active, r'\\server\share\game.mkv'), (active, '//server/share/game.mkv'),
                (active, 'game.mkv'), (active, str(Path(tempfile.gettempdir()) / 'notes.txt')),
                (active, None)):
            with self.subTest(outputs=outputs, path=path):
                self.assertIsNone(self.lookup(outputs, path))


class LiveClientTests(unittest.TestCase):
    def test_only_localhost(self):
        with self.assertRaises(ValueError):
            live_game_time("https://example.com:2999/liveclientdata/gamestats")

    def test_not_in_game_is_none(self):
        # Nothing listens on this port during tests, so the API "does not answer".
        self.assertIsNone(live_game_time("https://127.0.0.1:1/liveclientdata/gamestats", 0.2))


# ---------------------------------------------------------------- recorder state machine

class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.notes = Path(self.tmp.name) / "reviews.db"
        self.exe = Path(self.tmp.name) / "obs64.exe"
        self.exe.write_bytes(b"")
        self.obs, self.clock, self.launches = FakeObs(), Clock(), []
        self.reachable = True

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, probe, password="pw"):
        return Recorder(self.notes, password=password, probe=probe, obs_factory=self.obs,
                        launcher=self.launches.append, obs_exe=self.exe,
                        obs_reachable=lambda: self.reachable, clock=self.clock)

    def rows(self):
        with closing(sqlite3.connect(self.notes)) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute("SELECT * FROM recordings ORDER BY id")]

    def test_disarmed_by_default_and_never_polls(self):
        probe = Probe(100.0)
        rec = self.make(probe)
        self.assertEqual(rec.status(), {"armed": False, "obs": "running", "game": "idle",
                                        "last": None})
        self.assertFalse(rec.step())
        self.assertEqual((probe.calls, self.obs.requests, self.launches), (0, [], []))

    def test_full_game_start_offset_stop(self):
        probe = Probe(None, 95.0, 95.5, 400.0, *([None] * MISSES_TO_STOP))
        rec = self.make(probe)
        rec.armed = True
        self.assertTrue(rec.step())               # not in game
        self.assertEqual(self.obs.requests, [])
        rec.step()                                # game answers → StartRecord
        self.assertEqual(self.obs.requests, ["StartRecord"])
        row, = self.rows()
        self.assertEqual((row["status"], row["offset_s"], row["path"]), ("recording", -95.5, None))
        self.assertEqual(row["game_zero_ms"], int((self.clock.t - 95.5) * 1000))
        self.assertEqual(rec.status()["game"], "in_game")
        rec.step()                                # still in game: no second StartRecord
        for _ in range(MISSES_TO_STOP):
            rec.step()
        self.assertEqual(self.obs.requests, ["StartRecord", "StopRecord"])
        row, = self.rows()
        self.assertEqual((row["status"], row["path"]), ("saved", r"C:\Videos\game.mkv"))
        status = rec.status()
        self.assertEqual((status["game"], status["last"]["status"]), ("idle", "saved"))
        self.assertIsNone(status["last"]["match_id"])

    def test_single_missed_poll_does_not_stop(self):
        rec = self.make(Probe(10.0, 10.0, None, 14.0))
        rec.armed = True
        for _ in range(4):
            rec.step()
        self.assertEqual(self.obs.requests, ["StartRecord"])

    def test_loading_clock_zero_waits_before_starting_capture(self):
        rec = self.make(Probe(0.0, 0.0, 0.0, 1.2, 1.4))
        rec.armed = True
        for _ in range(3):
            rec.step()
        self.assertEqual(self.obs.requests, [])
        self.assertEqual(self.rows(), [])
        rec.step()
        self.assertEqual(self.obs.requests, ['StartRecord'])
        self.assertEqual(self.rows()[0]['offset_s'], -1.4)

    def test_loading_cancelled_does_not_create_a_recording(self):
        rec = self.make(Probe(0.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        for _ in range(1 + MISSES_TO_STOP):
            rec.step()
        self.assertEqual(self.obs.requests, [])
        self.assertEqual(self.rows(), [])
        self.assertFalse(rec.in_game)

    def test_disarm_mid_game_still_stops_at_game_end(self):
        rec = self.make(Probe(50.0, 50.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        rec.armed = False
        self.assertTrue(rec.step())               # keeps polling while recording
        for _ in range(MISSES_TO_STOP):
            last = rec.step()
        self.assertFalse(last)
        self.assertEqual(self.obs.requests, ["StartRecord", "StopRecord"])
        self.assertEqual(self.rows()[0]["status"], "saved")

    def test_obs_not_running_is_launched_then_retried(self):
        self.reachable = False
        self.obs.unreachable = True
        rec = self.make(Probe(30.0, 32.0, 32.5))
        rec.armed = True
        rec.step()
        self.assertEqual(self.launches, [self.exe])
        self.assertEqual(self.rows(), [])
        self.assertEqual(rec.status()["obs"], "stopped")
        self.obs.unreachable, self.reachable = False, True
        rec.step()
        self.assertEqual(self.rows()[0]["offset_s"], -32.5)
        self.assertEqual(len(self.launches), 1)   # cooldown: no second launch

    def test_arming_launches_obs_once(self):
        self.reachable = False
        rec = self.make(Probe())
        rec.ensure_obs()
        rec.ensure_obs()
        self.assertEqual(self.launches, [self.exe])

    def test_missing_exe_or_password_is_unavailable(self):
        self.exe.unlink()
        self.reachable = False
        self.assertEqual(self.make(Probe()).status()["obs"], "unavailable")
        status = self.make(Probe(), password="").status()
        self.assertEqual(status["obs"], "unavailable")
        self.assertIn("OBS_WS_PASSWORD", status["error"])

    def test_auth_failure_records_one_failed_row_per_game(self):
        self.obs.fail = ObsAuthError("OBS rejected OBS_WS_PASSWORD")
        rec = self.make(Probe(5.0, 7.0, 9.0))
        rec.armed = True
        for _ in range(3):
            rec.step()
        row, = self.rows()
        self.assertEqual(row["status"], "failed")
        status = rec.status()
        self.assertEqual((status["obs"], status["last"]["status"]), ("unavailable", "failed"))
        self.assertIn("rejected", status["error"])

    def test_stop_failure_marks_failed(self):
        rec = self.make(Probe(5.0, 5.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        self.obs.fail = ObsError("StopRecord failed: NotActive")
        for _ in range(MISSES_TO_STOP):
            rec.step()
        row, = self.rows()
        self.assertEqual((row["status"], row["path"]), ("failed", None))

    def test_manual_obs_stop_preserves_the_original_recording(self):
        video = Path(self.tmp.name) / 'game.mkv'
        self.obs.output_path = str(video)
        rec = self.make(Probe(5.0, 5.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        self.assertEqual(self.rows()[0]['path'], str(video))
        video.write_bytes(b'finished recording')
        self.obs.recording = False
        self.obs.output_path = str(Path(self.tmp.name) / 'different-game.mkv')
        self.obs.fail = ObsError('OutputNotRunning', code=501)
        for _ in range(MISSES_TO_STOP):
            rec.step()
        row, = self.rows()
        self.assertEqual((row['status'], row['path'], row['error']), ('saved', str(video), None))
        self.assertEqual(row['note'], recorder.NOTE_STOPPED_OUTSIDE)
        self.assertEqual(rec.status()['error'], recorder.NOTE_STOPPED_OUTSIDE)

    def test_restart_outside_lolcoach_keeps_the_file_whose_offset_is_known(self):
        video = Path(self.tmp.name) / 'game.mkv'
        video.write_bytes(b'first part')
        self.obs.output_path = str(video)
        self.obs.stop_path = str(Path(self.tmp.name) / 'later.mkv')
        rec = self.make(Probe(5.0, 5.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        for _ in range(MISSES_TO_STOP):
            rec.step()
        row, = self.rows()
        self.assertEqual((row['status'], row['path'], row['note']),
                         ('saved', str(video), recorder.NOTE_RESTARTED))
        self.assertIn('StopRecord', self.obs.requests)  # the later file is still stopped

    def test_restart_with_missing_first_file_fails(self):
        self.obs.output_path = str(Path(self.tmp.name) / 'gone.mkv')
        self.obs.stop_path = str(Path(self.tmp.name) / 'later.mkv')
        rec = self.make(Probe(5.0, 5.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        for _ in range(MISSES_TO_STOP):
            rec.step()
        row, = self.rows()
        self.assertEqual((row['status'], row['note']), ('failed', recorder.NOTE_RESTARTED))

    def test_same_file_with_other_separators_is_not_a_restart(self):
        video = Path(self.tmp.name) / 'game.mkv'
        self.obs.output_path = str(video)
        self.obs.stop_path = video.as_posix()
        rec = self.make(Probe(5.0, 5.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        for _ in range(MISSES_TO_STOP):
            rec.step()
        row, = self.rows()
        self.assertEqual((row['status'], row['note']), ('saved', None))

    def test_failed_filename_lookup_still_records(self):
        for reply in (ObsError('NoOutput', code=600), {'outputSettings': None}, {}):
            with self.subTest(reply=reply):
                self.notes.unlink(missing_ok=True)
                self.obs.recording, self.obs.lookup_reply = False, reply
                rec = self.make(Probe(5.0, 5.0))
                rec.armed = True
                rec.step()
                row, = self.rows()
                self.assertEqual((row['status'], row['path']), ('recording', None))

    def test_manual_stop_with_missing_file_stays_failed(self):
        self.obs.output_path = str(Path(self.tmp.name) / 'missing.mkv')
        rec = self.make(Probe(5.0, 5.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        self.obs.fail = ObsError('OutputNotRunning', code=501)
        for _ in range(MISSES_TO_STOP):
            rec.step()
        self.assertEqual(self.rows()[0]['status'], 'failed')

    def test_other_stop_errors_are_not_hidden_by_an_existing_file(self):
        video = Path(self.tmp.name) / 'incomplete.mkv'
        video.write_bytes(b'partial recording')
        self.obs.output_path = str(video)
        rec = self.make(Probe(5.0, 5.0, *([None] * MISSES_TO_STOP)))
        rec.armed = True
        rec.step()
        self.obs.fail = ObsError('Output failure', code=702)
        for _ in range(MISSES_TO_STOP):
            rec.step()
        self.assertEqual(self.rows()[0]['status'], 'failed')

    def test_inactive_start_never_publishes_a_recording_offset(self):
        from unittest import mock
        rec = self.make(Probe(5.0))
        rec.armed = True
        with mock.patch.object(recorder, 'wait_for_recording', side_effect=ObsError('inactive')):
            rec.step()
        row, = self.rows()
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['offset_s'])
        self.assertIsNone(rec.recording_id)

    def test_thread_arms_and_disarms(self):
        rec = self.make(Probe())
        rec.poll_s = 0.01
        self.assertTrue(rec.set_armed(True)["armed"])
        thread = rec.thread
        self.assertFalse(rec.set_armed(False)["armed"])
        thread.join(2)
        self.assertFalse(thread.is_alive())


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.league = sqlite3.connect(":memory:")
        self.league.executescript(SCHEMA)
        self.notes = sqlite3.connect(":memory:")
        recorder.ensure_schema(self.notes)
        self.addCleanup(self.league.close)
        self.addCleanup(self.notes.close)

    def add_match(self, match_id, start):
        store_match(self.league, riot_match(match_id, "Ahri", True, start=start), ME)

    def add_rec(self, zero, status="saved"):
        self.notes.execute("INSERT INTO recordings (path, offset_s, status, game_zero_ms) "
                           "VALUES ('v.mkv', -10, ?, ?)", (status, zero))

    def test_nearest_match_within_window(self):
        self.add_match("EARLY", 1_000_000)
        self.add_match("NEAR", 1_000_000 + 20 * 60_000)
        self.add_rec(1_000_000 + 21 * 60_000)           # clock zero ~1 min after start
        self.add_rec(1_000_000 + 60 * 60_000)           # no match fetched yet
        self.add_rec(1_000_000, status="recording")     # still in progress: never linked
        self.assertEqual(link_recordings(self.notes, self.league), 1)
        rows = self.notes.execute("SELECT match_id, status FROM recordings ORDER BY id").fetchall()
        self.assertEqual(rows, [("NEAR", "linked"), (None, "saved"), (None, "recording")])
        self.assertEqual(link_recordings(self.notes, self.league), 0)

    def test_one_match_links_once(self):
        self.add_match("A", 5_000_000)
        self.add_rec(5_060_000)
        self.add_rec(5_090_000)
        link_recordings(self.notes, self.league)
        self.assertEqual(self.notes.execute(
            "SELECT COUNT(*) FROM recordings WHERE match_id='A'").fetchone()[0], 1)

    def test_recording_lookup_excludes_unfinished_and_failed_files(self):
        for status in ('recording', 'failed', 'linked'):
            with self.subTest(status=status):
                self.notes.execute('DELETE FROM recordings')
                self.add_rec(5_000_000, status=status)
                self.notes.execute("UPDATE recordings SET match_id='A'")
                result = recorder.recording_for(self.notes, 'A')
                self.assertEqual(result is not None, status == 'linked')


class SchemaTests(unittest.TestCase):
    def test_older_recordings_table_gains_a_note_column(self):
        with closing(sqlite3.connect(':memory:')) as conn:
            conn.execute("CREATE TABLE recordings (id INTEGER PRIMARY KEY, match_id TEXT, "
                         "path TEXT, offset_s REAL, started_at TEXT, status TEXT NOT NULL, "
                         "game_zero_ms INTEGER, error TEXT)")
            recorder.ensure_schema(conn)
            recorder.ensure_schema(conn)  # idempotent
            columns = [r[1] for r in conn.execute('PRAGMA table_info(recordings)')]
        self.assertEqual(columns[-1], 'note')


class RecordingStoreTests(unittest.TestCase):
    def test_saved_recording_links_without_a_review_server(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, notes, video = root / 'league.db', root / 'reviews.db', root / 'game.mp4'
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript(SCHEMA)
                store_match(conn, riot_match('M1', 'Ahri', True, start=1_000_000), ME)
                conn.execute("INSERT INTO timelines VALUES ('M1', 60000, '{}')")
                conn.commit()
            video.write_bytes(b'fixture')
            store = ReviewStore(db, notes)
            self.assertIsNone(store.recording('M1'))
            with closing(sqlite3.connect(notes)) as conn, conn:
                conn.execute("INSERT INTO recordings (path, offset_s, started_at, status, game_zero_ms) "
                             "VALUES (?, -12.5, '2026-09-22T20:00:00+00:00', 'saved', ?)",
                             (str(video), 1_090_000))
            recording = store.recording('M1')
            self.assertEqual(recording['offset_s'], -12.5)
            self.assertEqual(recording['status'], 'linked')
            self.assertEqual(store.recording_path('M1'), video)
            self.assertIsNone(recording['url'])
            self.assertEqual(store.detail('M1')['recording'], recording)


class RecorderCliTests(unittest.TestCase):
    def test_cli_link_opens_matches_readonly(self):
        from unittest import mock
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db, notes = root / 'league.db', root / 'reviews.db'
            with closing(sqlite3.connect(db)) as conn:
                conn.executescript(SCHEMA)
                store_match(conn, riot_match('M1', 'Ahri', True, start=1_000_000), ME)
            with closing(sqlite3.connect(notes)) as conn, conn:
                recorder.ensure_schema(conn)
                conn.execute("INSERT INTO recordings (path, status, game_zero_ms) VALUES ('game.mkv', 'saved', 1090000)")
            before = db.read_bytes()
            with mock.patch('fetch_matches.load_dotenv'), redirect_stdout(io.StringIO()):
                recorder.main(['--link', '--db', str(db), '--notes', str(notes)])
            self.assertEqual(db.read_bytes(), before)
            with closing(sqlite3.connect(notes)) as conn:
                self.assertEqual(conn.execute('SELECT match_id, status FROM recordings').fetchone(), ('M1', 'linked'))


if __name__ == '__main__':
    unittest.main()
