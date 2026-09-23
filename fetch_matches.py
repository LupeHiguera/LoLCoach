#!/usr/bin/env python3
"""
Fetch your recent games for chosen champions (default: Ahri, Zoe) from the
Riot API into a local SQLite database, then print a lane summary at 10 minutes.

Every match scanned is stored (any champion), so later projects such as a
session/tilt tracker can reuse them. Timelines are only fetched for games on
the target champions. Already-stored data is never re-downloaded.

Usage:
    python fetch_matches.py                      # uses RIOT_API_KEY / RIOT_ID from .env
    python fetch_matches.py --count 20 --queue 420
    python fetch_matches.py --summary-only
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

DEFAULT_CHAMPIONS = ["Ahri", "Zoe"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id           TEXT PRIMARY KEY,
    game_start_ms      INTEGER,
    duration_s         INTEGER,
    patch              TEXT,
    queue_id           INTEGER,
    my_participant_id  INTEGER,
    my_champion        TEXT,
    my_position        TEXT,
    win                INTEGER,
    opp_participant_id INTEGER,
    opp_champion       TEXT,
    raw_json           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS participants (
    match_id       TEXT,
    participant_id INTEGER,
    puuid          TEXT,
    champion       TEXT,
    team_id        INTEGER,
    position       TEXT,
    win            INTEGER,
    kills          INTEGER,
    deaths         INTEGER,
    assists        INTEGER,
    cs             INTEGER,
    gold_earned    INTEGER,
    PRIMARY KEY (match_id, participant_id)
);
CREATE TABLE IF NOT EXISTS timelines (
    match_id          TEXT PRIMARY KEY,
    frame_interval_ms INTEGER,
    raw_json          TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS frames (
    match_id       TEXT,
    participant_id INTEGER,
    timestamp_ms   INTEGER,
    total_gold     INTEGER,
    current_gold   INTEGER,
    xp             INTEGER,
    level          INTEGER,
    minions        INTEGER,
    jungle_minions INTEGER,
    x              INTEGER,
    y              INTEGER,
    PRIMARY KEY (match_id, participant_id, timestamp_ms)
);
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id       TEXT,
    timestamp_ms   INTEGER,
    type           TEXT,
    participant_id INTEGER,
    killer_id      INTEGER,
    victim_id      INTEGER,
    assisting_ids  TEXT,
    x              INTEGER,
    y              INTEGER,
    raw_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_match ON events (match_id, type);
"""


# ---------------------------------------------------------------- config

def load_dotenv(path=".env"):
    """Minimal .env reader so there are no extra dependencies."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- API client

class RiotClient:
    # Development-key limits are 20 requests/1s and 100 requests/2min.
    # Stay slightly under both.
    LIMITS = [(19, 1.0), (95, 120.0)]

    def __init__(self, api_key, region):
        # Imported here so analyze.py and review_app.py stay standard-library only.
        import requests
        self.base = f"https://{region}.api.riotgames.com"
        self.session = requests.Session()
        self.session.headers["X-Riot-Token"] = api_key
        self.calls = deque()

    def _throttle(self):
        while True:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > 120:
                self.calls.popleft()
            wait = 0.0
            for limit, window in self.LIMITS:
                recent = [t for t in self.calls if now - t < window]
                if len(recent) >= limit:
                    wait = max(wait, window - (now - recent[0]))
            if wait <= 0:
                break
            if wait > 2:
                print(f"  (pausing {wait:.0f}s for rate limit)", file=sys.stderr)
            time.sleep(wait + 0.05)
        self.calls.append(time.monotonic())

    def get(self, path, params=None):
        for attempt in range(5):
            self._throttle()
            r = self.session.get(self.base + path, params=params, timeout=30)
            if r.status_code == 429:
                retry = int(r.headers.get("Retry-After", "10"))
                print(f"  rate limited by server, waiting {retry}s", file=sys.stderr)
                time.sleep(retry)
                continue
            if r.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            if r.status_code in (401, 403):
                sys.exit("API key rejected (401/403). Development keys expire every "
                         "24 hours; generate a new one at developer.riotgames.com.")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"Giving up on {path} after repeated errors")


# ---------------------------------------------------------------- storing

def store_match(conn, match, puuid):
    info = match["info"]
    match_id = match["metadata"]["matchId"]
    parts = info["participants"]

    me = next((p for p in parts if p["puuid"] == puuid), None)
    if me is None:
        return None
    my_pos = me.get("teamPosition") or me.get("individualPosition") or ""
    opp = None
    if my_pos and my_pos != "Invalid":
        opp = next((p for p in parts
                    if p["teamId"] != me["teamId"]
                    and (p.get("teamPosition") or p.get("individualPosition")) == my_pos), None)

    patch = ".".join(info.get("gameVersion", "").split(".")[:2])
    conn.execute(
        "INSERT OR REPLACE INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (match_id, info.get("gameStartTimestamp"), info.get("gameDuration"), patch,
         info.get("queueId"), me["participantId"], me["championName"], my_pos,
         int(me["win"]), opp["participantId"] if opp else None,
         opp["championName"] if opp else None, json.dumps(match)))

    for p in parts:
        conn.execute(
            "INSERT OR REPLACE INTO participants VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (match_id, p["participantId"], p["puuid"], p["championName"], p["teamId"],
             p.get("teamPosition") or p.get("individualPosition"), int(p["win"]),
             p["kills"], p["deaths"], p["assists"],
             p.get("totalMinionsKilled", 0) + p.get("neutralMinionsKilled", 0),
             p.get("goldEarned")))
    conn.commit()
    return me["championName"]


def store_timeline(conn, match_id, timeline):
    info = timeline["info"]
    conn.execute("DELETE FROM frames WHERE match_id = ?", (match_id,))
    conn.execute("DELETE FROM events WHERE match_id = ?", (match_id,))
    conn.execute("INSERT OR REPLACE INTO timelines VALUES (?,?,?)",
                 (match_id, info.get("frameInterval"), json.dumps(timeline)))

    for frame in info["frames"]:
        ts = frame["timestamp"]
        for pid, pf in frame["participantFrames"].items():
            pos = pf.get("position") or {}
            conn.execute(
                "INSERT OR REPLACE INTO frames VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (match_id, int(pid), ts, pf.get("totalGold"), pf.get("currentGold"),
                 pf.get("xp"), pf.get("level"), pf.get("minionsKilled"),
                 pf.get("jungleMinionsKilled"), pos.get("x"), pos.get("y")))
        for e in frame.get("events", []):
            pos = e.get("position") or {}
            conn.execute(
                "INSERT INTO events (match_id, timestamp_ms, type, participant_id, killer_id,"
                " victim_id, assisting_ids, x, y, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (match_id, e.get("timestamp"), e.get("type"),
                 e.get("participantId") or e.get("creatorId"),
                 e.get("killerId"), e.get("victimId"),
                 json.dumps(e["assistingParticipantIds"]) if "assistingParticipantIds" in e else None,
                 pos.get("x"), pos.get("y"), json.dumps(e)))
    conn.commit()


# ---------------------------------------------------------------- fetching

def fetch(conn, client, puuid, champions, count, max_scan, queue):
    found = scanned = start = 0
    while found < count and scanned < max_scan:
        params = {"start": start, "count": 100}
        if queue:
            params["queue"] = queue
        ids = client.get(f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params)
        if not ids:
            break
        for match_id in ids:
            if found >= count or scanned >= max_scan:
                break
            scanned += 1
            row = conn.execute("SELECT my_champion FROM matches WHERE match_id = ?",
                               (match_id,)).fetchone()
            if row:
                champ = row[0]
            else:
                match = client.get(f"/lol/match/v5/matches/{match_id}")
                champ = store_match(conn, match, puuid) if match else None
            if champ not in champions:
                continue
            found += 1
            have_tl = conn.execute("SELECT 1 FROM timelines WHERE match_id = ?",
                                   (match_id,)).fetchone()
            status = "cached"
            if not have_tl:
                tl = client.get(f"/lol/match/v5/matches/{match_id}/timeline")
                if tl:
                    store_timeline(conn, match_id, tl)
                    status = "fetched"
                else:
                    status = "no timeline available"
            print(f"  [{found}/{count}] {match_id} {champ}: {status}")
        start += len(ids)
    print(f"\nScanned {scanned} matches, found {found} on {', '.join(champions)}.")


# ---------------------------------------------------------------- summary

def value_at(conn, match_id, pid, minute):
    target = minute * 60_000
    return conn.execute(
        "SELECT timestamp_ms, total_gold, xp, minions + jungle_minions FROM frames "
        "WHERE match_id = ? AND participant_id = ? ORDER BY ABS(timestamp_ms - ?) LIMIT 1",
        (match_id, pid, target)).fetchone()


def fmt(n):
    return "   -" if n is None else f"{n:+5d}"


def print_summary(conn, champions, minute=10):
    marks = ",".join("?" * len(champions))
    rows = conn.execute(
        f"SELECT m.match_id, m.game_start_ms, m.my_champion, m.opp_champion, m.win, "
        f"m.my_participant_id, m.opp_participant_id, m.patch "
        f"FROM matches m JOIN timelines t USING (match_id) "
        f"WHERE m.my_champion IN ({marks}) ORDER BY m.game_start_ms DESC",
        champions).fetchall()
    if not rows:
        print("No games with timelines stored yet.")
        return

    print(f"\nLane differences at {minute}:00 vs your lane opponent (you minus them)")
    print(f"{'date':<11}{'patch':<7}{'matchup':<24}{'res':<5}{'gold':>6}{'xp':>6}{'cs':>6}"
          f"{'deaths<15':>11}")
    for (mid, start, champ, opp, win, me, opp_id, patch) in rows:
        date = datetime.fromtimestamp(start / 1000).strftime("%Y-%m-%d") if start else "?"
        gd = xd = cd = None
        if opp_id:
            a, b = value_at(conn, mid, me, minute), value_at(conn, mid, opp_id, minute)
            if a and b and abs(a[0] - minute * 60_000) <= 30_000:
                gd, xd, cd = a[1] - b[1], a[2] - b[2], a[3] - b[3]
        deaths = conn.execute(
            "SELECT COUNT(*) FROM events WHERE match_id = ? AND type = 'CHAMPION_KILL' "
            "AND victim_id = ? AND timestamp_ms < 900000", (mid, me)).fetchone()[0]
        matchup = f"{champ} vs {opp or '?'}"
        print(f"{date:<11}{patch:<7}{matchup:<24}{'W' if win else 'L':<5}"
              f"{fmt(gd):>6}{fmt(xd):>6}{fmt(cd):>6}{deaths:>11}")


# ---------------------------------------------------------------- main

def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--riot-id", default=os.getenv("RIOT_ID"), help="Name#TAG")
    ap.add_argument("--region", default=os.getenv("RIOT_REGION", "americas"),
                    help="americas, europe, asia or sea (default: americas)")
    ap.add_argument("--champions", default=",".join(DEFAULT_CHAMPIONS),
                    help="Comma-separated champion names as the API spells them")
    ap.add_argument("--count", type=int, default=20, help="Target games to collect")
    ap.add_argument("--max-scan", type=int, default=200,
                    help="Stop after checking this many matches")
    ap.add_argument("--queue", type=int, default=None,
                    help="Optional queue filter, e.g. 420 = ranked solo/duo, 440 = flex")
    ap.add_argument("--db", default="league.db")
    ap.add_argument("--summary-only", action="store_true")
    args = ap.parse_args()

    champions = [c.strip() for c in args.champions.split(",") if c.strip()]
    conn = sqlite3.connect(args.db)
    conn.executescript(SCHEMA)

    if not args.summary_only:
        api_key = os.getenv("RIOT_API_KEY")
        if not api_key:
            sys.exit("Set RIOT_API_KEY in .env or your environment.")
        if not args.riot_id or "#" not in args.riot_id:
            sys.exit("Set RIOT_ID (like Name#NA1) in .env or pass --riot-id.")
        name, tag = args.riot_id.rsplit("#", 1)

        client = RiotClient(api_key, args.region)
        account = client.get(
            f"/riot/account/v1/accounts/by-riot-id/{quote(name)}/{quote(tag)}")
        if not account:
            sys.exit(f"Riot ID {args.riot_id} not found in region {args.region}.")
        print(f"Found {args.riot_id}. Looking for {', '.join(champions)} games...")
        fetch(conn, client, account["puuid"], champions, args.count, args.max_scan, args.queue)

    print_summary(conn, champions)
    conn.close()


if __name__ == "__main__":
    main()
