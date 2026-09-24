#!/usr/bin/env python3
"""
Lane analysis over the database that fetch_matches.py builds.

Reads only; it never calls the Riot API, so it works on an expired key.

    python analyze.py                    # every section
    python analyze.py matchups
    python analyze.py backs --champions Ahri
    python analyze.py roams --minute 15
    python analyze.py deaths --matchup Syndra

Sections:
    matchups  per-opponent averages at 10:00, plus first-back and early deaths
    backs     first recall timing per game, and whether it was forced by a death
    roams     minutes spent off the mid lane before 15:00, and what they cost
    deaths    ASCII map of where you died before 15:00
    charm     Ahri Charm estimate: enemy immobilizations per E cast
    profile   end-of-game habits per champion (e.g. Ahri next to Lulu)
"""
import argparse
import json
import math
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fetch_matches import DEFAULT_CHAMPIONS, value_at

# Summoner's Rift timeline coordinates run roughly 0..15000 on both axes,
# with blue base at the origin corner and red base at the far corner.
MAP_MAX = 15000
FOUNTAIN = {100: (560, 580), 200: (14300, 14300)}

# A champion within this perpendicular distance of the x==y diagonal is
# treated as standing in mid lane. Roughly half a lane plus the brush either side.
MID_CORRIDOR = 2200
BASE_RADIUS = 2400

# Riot's timeline event names. ITEM_PURCHASED is past tense; ITEM_PURCHASE
# silently matches nothing.
EV_PURCHASE = "ITEM_PURCHASED"
EV_KILL = "CHAMPION_KILL"

# Purchases before this are the opening build, not a recall.
OPENING_MS = 120_000
# Purchases closer together than this are one trip to the shop.
SHOP_GAP_MS = 12_000
# A death this soon before a shop visit means the "back" was really a respawn.
DEATH_WINDOW_MS = 25_000

# Bootstrap confidence intervals: resample whole games with replacement.
# A fixed seed makes the same data always print the same interval.
CI_ITERATIONS = 2000
CI_LEVEL = 0.90
CI_SEED = 1337
# Below this many games an interval is not computed at all.
CI_MIN_GAMES = 2


# ---------------------------------------------------------------- geometry

def lane_offset(x, y):
    """Signed distance from the mid diagonal: >0 toward top lane, <0 toward bot."""
    return (y - x) / math.sqrt(2)


def distance(x, y, point):
    return math.hypot(x - point[0], y - point[1])


def off_lane(x, y):
    """True when this position is neither in mid lane nor inside either base."""
    if x is None or y is None:
        return False
    if abs(lane_offset(x, y)) <= MID_CORRIDOR:
        return False
    return (distance(x, y, FOUNTAIN[100]) > BASE_RADIUS
            and distance(x, y, FOUNTAIN[200]) > BASE_RADIUS)


# ---------------------------------------------------------------- selection

def games(conn, champions, matchup=None):
    """Matches on the target champions that have a timeline and a known opponent."""
    marks = ",".join("?" * len(champions))
    sql = (f"SELECT m.match_id, m.game_start_ms, m.my_champion, m.opp_champion, m.win, "
           f"m.my_participant_id, m.opp_participant_id, m.patch, m.duration_s "
           f"FROM matches m JOIN timelines t USING (match_id) "
           f"WHERE m.my_champion IN ({marks}) AND m.opp_participant_id IS NOT NULL")
    params = list(champions)
    if matchup:
        sql += " AND m.opp_champion LIKE ?"
        params.append(f"%{matchup}%")
    return conn.execute(sql + " ORDER BY m.game_start_ms DESC", params).fetchall()


def open_db(path):
    """Open the database read-only, with a clear message if it isn't there yet."""
    db = Path(path)
    if not db.exists():
        sys.exit(f"No database at {path}. Run fetch_matches.py first to create it.")
    conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    missing = {"matches", "frames", "events", "timelines"} - {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if missing:
        sys.exit(f"{path} is missing table(s): {', '.join(sorted(missing))}. "
                 f"Is it a fetch_matches.py database?")
    return conn


def team_of(conn, match_id, pid):
    row = conn.execute("SELECT team_id FROM participants WHERE match_id = ? AND "
                       "participant_id = ?", (match_id, pid)).fetchone()
    return row[0] if row else (100 if pid <= 5 else 200)


def lane_diffs(conn, match_id, me, opp, minute):
    """(gold, xp, cs) difference at `minute`, or Nones if no frame lands near it."""
    a, b = value_at(conn, match_id, me, minute), value_at(conn, match_id, opp, minute)
    if not (a and b):
        return None, None, None
    if abs(a[0] - minute * 60_000) > 30_000 or abs(b[0] - minute * 60_000) > 30_000:
        return None, None, None
    return a[1] - b[1], a[2] - b[2], a[3] - b[3]


# ---------------------------------------------------------------- shop trips

def shop_visits(conn, match_id, pid):
    """Purchase timestamps grouped into individual trips to the shop."""
    stamps = [r[0] for r in conn.execute(
        "SELECT timestamp_ms FROM events WHERE match_id = ? AND type = ? "
        "AND participant_id = ? ORDER BY timestamp_ms", (match_id, EV_PURCHASE, pid))]
    visits = []
    for t in stamps:
        if visits and t - visits[-1][-1] <= SHOP_GAP_MS:
            visits[-1].append(t)
        else:
            visits.append([t])
    return visits


def first_back(conn, match_id, pid):
    """(timestamp_ms, forced_by_death) of the first recall, or (None, False)."""
    for visit in shop_visits(conn, match_id, pid):
        if visit[0] < OPENING_MS:
            continue
        died = conn.execute(
            "SELECT 1 FROM events WHERE match_id = ? AND type = ? "
            "AND victim_id = ? AND timestamp_ms BETWEEN ? AND ? LIMIT 1",
            (match_id, EV_KILL, pid, visit[0] - DEATH_WINDOW_MS, visit[0])).fetchone()
        return visit[0], died is not None
    return None, False


# ---------------------------------------------------------------- roaming

def roam_windows(conn, match_id, pid, until_minute):
    """Stretches of consecutive frames spent away from mid, before `until_minute`.

    Frames are one minute apart, so anything shorter than about a minute is
    invisible here; these are the long absences, not every wander into the river.
    """
    rows = conn.execute(
        "SELECT timestamp_ms, minions + jungle_minions, x, y FROM frames "
        "WHERE match_id = ? AND participant_id = ? AND timestamp_ms <= ? "
        "ORDER BY timestamp_ms", (match_id, pid, until_minute * 60_000)).fetchall()

    cs_at = {ts // 60_000: cs for ts, cs, _, _ in rows}
    windows = []
    for ts, _cs, x, y in rows:
        minute = ts // 60_000
        if minute < 2 or not off_lane(x, y):
            continue
        if windows and windows[-1]["end"] == minute - 1:
            windows[-1]["end"] = minute
            windows[-1]["offsets"].append(lane_offset(x, y))
        else:
            windows.append({"start": minute, "end": minute,
                            "offsets": [lane_offset(x, y)]})

    for w in windows:
        before = cs_at.get(w["start"] - 1)
        after = cs_at.get(w["end"])
        w["minutes"] = w["end"] - w["start"] + 1
        w["cs"] = (after - before) if (before is not None and after is not None) else None
        w["side"] = "top" if sum(w["offsets"]) > 0 else "bot"
        w["kills"] = kill_participation(conn, match_id, pid,
                                        (w["start"] - 1) * 60_000, (w["end"] + 1) * 60_000)
    return windows


def kill_participation(conn, match_id, pid, from_ms, to_ms):
    """Kills you landed or assisted in a time range."""
    n = 0
    for killer, assists in conn.execute(
            "SELECT killer_id, assisting_ids FROM events WHERE match_id = ? "
            "AND type = ? AND timestamp_ms BETWEEN ? AND ?",
            (match_id, EV_KILL, from_ms, to_ms)):
        if killer == pid:
            n += 1
        elif assists:
            try:
                if pid in json.loads(assists):
                    n += 1
            except (ValueError, TypeError):
                pass
    return n


def lane_cs_rate(conn, match_id, pid, until_minute, roams):
    """CS per minute during the minutes you were actually in lane."""
    roamed = {m for w in roams for m in range(w["start"], w["end"] + 1)}
    rows = conn.execute(
        "SELECT timestamp_ms, minions + jungle_minions FROM frames WHERE match_id = ? "
        "AND participant_id = ? AND timestamp_ms <= ? ORDER BY timestamp_ms",
        (match_id, pid, until_minute * 60_000)).fetchall()
    cs_at = {ts // 60_000: cs for ts, cs in rows}
    gained = spent = 0
    for minute in sorted(cs_at):
        if minute == 0 or minute in roamed or (minute - 1) in roamed:
            continue
        if minute - 1 in cs_at:
            gained += cs_at[minute] - cs_at[minute - 1]
            spent += 1
    return gained / spent if spent else None


# ---------------------------------------------------------------- formatting

def mmss(ms):
    if ms is None:
        return "   -"
    return f"{int(ms // 60000)}:{int(ms // 1000) % 60:02d}"


def signed(n, width=5):
    return "-".rjust(width) if n is None else f"{n:+{width}d}"


def mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def num(value, width=6, places=1):
    return "-".rjust(width) if value is None else f"{value:{width}.{places}f}"


# ---------------------------------------------------------------- sections

def collect(conn, champions, minute, matchup):
    """One record per analysable game, keyed for grouping by matchup."""
    out = []
    for (mid, start, champ, opp, win, me, opp_id, patch, dur) in games(
            conn, champions, matchup):
        gd, xd, cd = lane_diffs(conn, mid, me, opp_id, minute)
        back, forced = first_back(conn, mid, me)
        opp_back, _ = first_back(conn, mid, opp_id)
        roams = roam_windows(conn, mid, me, 15)
        deaths = conn.execute(
            "SELECT COUNT(*) FROM events WHERE match_id = ? AND type = ? "
            "AND victim_id = ? AND timestamp_ms < 900000",
            (mid, EV_KILL, me)).fetchone()[0]
        out.append({
            "match_id": mid, "date": start, "champ": champ, "opp": opp, "win": win,
            "me": me, "opp_id": opp_id, "patch": patch, "duration": dur,
            "gd": gd, "xd": xd, "cd": cd, "deaths": deaths,
            "back": back, "forced": forced, "opp_back": opp_back,
            "roams": roams,
            "lane_cs": lane_cs_rate(conn, mid, me, 15, roams),
        })
    return out


def by_matchup(records):
    groups = defaultdict(list)
    for r in records:
        groups[f"{r['champ']} vs {r['opp']}"].append(r)
    return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))


def stat_line(label, rows):
    wins = sum(r["win"] for r in rows)
    print(f"{label:<24}{len(rows):>3}{f'{wins}-{len(rows) - wins}':>7}"
          f"{num(mean([r['gd'] for r in rows]), 8, 0)}"
          f"{num(mean([r['xd'] for r in rows]), 8, 0)}"
          f"{num(mean([r['cd'] for r in rows]), 7, 1)}"
          f"{num(mean([r['deaths'] for r in rows]), 11, 1)}"
          f"{mmss(mean([r['back'] for r in rows])):>10}")


def section_matchups(records, minute, min_games=1):
    print(f"\nMatchups - averages at {minute}:00, you minus your lane opponent")
    print(f"{'matchup':<24}{'n':>3}{'W-L':>7}{'gold':>8}{'xp':>8}{'cs':>7}"
          f"{'deaths<15':>11}{'1st back':>10}")
    print("-" * 78)
    hidden = 0
    for name, rows in by_matchup(records):
        if len(rows) < min_games:
            hidden += len(rows)
            continue
        stat_line(name, rows)
    if hidden:
        print(f"({hidden} games in matchups played fewer than {min_games} times)")

    # Single games are mostly noise, so roll everything up per champion too.
    print("-" * 78)
    champs = defaultdict(list)
    for r in records:
        champs[r["champ"]].append(r)
    for champ, rows in sorted(champs.items(), key=lambda kv: -len(kv[1])):
        stat_line(f"{champ}, all matchups", rows)


def section_backs(records):
    print("\nFirst recall - when you left lane, and whether you chose to")
    print(f"{'date':<11}{'matchup':<24}{'res':<5}{'yours':>7}{'theirs':>8}"
          f"{'delta':>7}  note")
    print("-" * 78)
    for r in sorted(records, key=lambda r: r["date"] or 0, reverse=True):
        date = (datetime.fromtimestamp(r["date"] / 1000).strftime("%Y-%m-%d")
                if r["date"] else "?")
        delta = ("-" if r["back"] is None or r["opp_back"] is None
                 else f"{(r['back'] - r['opp_back']) / 1000:+.0f}s")
        note = "died first" if r["forced"] else ""
        print(f"{date:<11}{r['champ'] + ' vs ' + (r['opp'] or '?'):<24}"
              f"{'W' if r['win'] else 'L':<5}{mmss(r['back']):>7}"
              f"{mmss(r['opp_back']):>8}{delta:>7}  {note}")

    forced = [r for r in records if r["forced"]]
    clean = [r["back"] for r in records if not r["forced"] and r["back"]]
    print(f"\n  {len(forced)} of {len(records)} first backs followed a death.")
    if clean:
        print(f"  Median clean first back: {mmss(sorted(clean)[len(clean) // 2])}")


def section_roams(records):
    print("\nRoams - minutes spent off mid before 15:00")
    print(f"{'matchup':<24}{'n':>3}{'roams/g':>9}{'min/g':>8}"
          f"{'cs in lane':>12}{'cs on roam':>12}{'kills':>7}")
    print("-" * 78)
    for name, rows in by_matchup(records):
        windows = [w for r in rows for w in r["roams"]]
        roam_minutes = sum(w["minutes"] for w in windows)
        roam_cs = [w["cs"] / w["minutes"] for w in windows
                   if w["cs"] is not None and w["minutes"]]
        print(f"{name:<24}{len(rows):>3}{len(windows) / len(rows):>9.1f}"
              f"{roam_minutes / len(rows):>8.1f}"
              f"{num(mean([r['lane_cs'] for r in rows]), 12, 1)}"
              f"{num(mean(roam_cs), 12, 1)}"
              f"{sum(w['kills'] for w in windows):>7}")

    windows = [w for r in records for w in r["roams"]]
    if not windows:
        print("\n  No off-lane stretches long enough to show at 1-minute resolution.")
        return
    dry = [w for w in windows if not w["kills"]]
    top = sum(1 for w in windows if w["side"] == "top")
    print(f"\n  {len(windows)} roams total: {top} topside, {len(windows) - top} botside.")
    print(f"  {len(dry)} ({100 * len(dry) // len(windows)}%) produced no kill or assist.")


def section_deaths(conn, records, size=21):
    print("\nDeaths before 15:00 - map view (red base top-right, blue bottom-left)")
    for name, rows in by_matchup(records):
        points = []
        for r in rows:
            points += conn.execute(
                "SELECT x, y FROM events WHERE match_id = ? AND type = ? "
                "AND victim_id = ? AND timestamp_ms < 900000 AND x IS NOT NULL",
                (r["match_id"], EV_KILL, r["me"])).fetchall()
        print(f"\n  {name}  -  {len(points)} deaths over {len(rows)} games")
        if not points:
            print("    (none)")
            continue
        grid = [[0] * size for _ in range(size)]
        for x, y in points:
            col = min(size - 1, max(0, int(x / MAP_MAX * size)))
            row = min(size - 1, max(0, int((1 - y / MAP_MAX) * size)))
            grid[row][col] += 1
        for r_i, line in enumerate(grid):
            cells = []
            for c_i, n in enumerate(line):
                if n:
                    cells.append(str(n) if n < 10 else "#")
                else:
                    # faint guide along the mid diagonal
                    cells.append("." if abs((size - 1 - r_i) - c_i) <= 1 else " ")
            print("    |" + " ".join(cells) + "|")


# ---------------------------------------------------------------- uncertainty

def bootstrap_ci(games, statistic, iterations=CI_ITERATIONS, level=CI_LEVEL, seed=CI_SEED):
    """Percentile bootstrap interval for `statistic` over a list of games.

    Games are resampled with replacement, so the interval reflects game-to-game
    variation in *your* sample. It says how much the number could move with
    different games like these; it does not correct for matchups, patches or a
    biased sample, and it is None below CI_MIN_GAMES games.
    """
    if len(games) < CI_MIN_GAMES:
        return None
    rng = random.Random(seed)
    values = []
    for _ in range(iterations):
        value = statistic(rng.choices(games, k=len(games)))
        if value is not None:
            values.append(value)
    if not values:
        return None
    values.sort()
    tail = (1 - level) / 2
    lo = values[int(tail * (len(values) - 1))]
    hi = values[int(math.ceil((1 - tail) * (len(values) - 1)))]
    return [lo, hi]


def ci_method():
    """How intervals are computed, for captions next to the numbers."""
    return {"ci_level": CI_LEVEL, "iterations": CI_ITERATIONS, "resample": "game"}


def pct_range(ci, width=12):
    if ci is None:
        return "-".rjust(width)
    return f"{100 * ci[0]:.0f}-{100 * ci[1]:.0f}%".rjust(width)


def num_range(ci, width=12, places=1):
    if ci is None:
        return "-".rjust(width)
    return f"{ci[0]:.{places}f}-{ci[1]:.{places}f}".rjust(width)


# ---------------------------------------------------------------- charm

def my_stats(conn, champions=None):
    """(match row, my participant dict) for every stored match, oldest first.

    Uses matches.raw_json, so games without a timeline (e.g. Lulu) are included.
    """
    sql = ("SELECT match_id, game_start_ms, my_champion, opp_champion, win, "
           "duration_s, my_participant_id, raw_json FROM matches")
    params = []
    if champions:
        sql += f" WHERE my_champion IN ({','.join('?' * len(champions))})"
        params = list(champions)
    out = []
    for mid, start, champ, opp, win, dur, pid, raw in conn.execute(
            sql + " ORDER BY game_start_ms", params):
        try:
            parts = json.loads(raw)["info"]["participants"]
        except (ValueError, KeyError, TypeError):
            continue
        me = next((p for p in parts if p.get("participantId") == pid), None)
        if me is not None:
            out.append(({"match_id": mid, "date": start, "champ": champ, "opp": opp,
                         "win": win, "duration": dur}, me))
    return out


def charm_records(conn):
    """Per-game Charm estimate for Ahri games.

    Charm is Ahri's only crowd control, so Riot's enemyChampionImmobilizations
    challenge stat divided by E casts approximates a Charm hit rate. It is an
    estimate from end-of-game totals, not a log of individual casts.
    """
    out = []
    for game, me in my_stats(conn, ["Ahri"]):
        casts = me.get("spell3Casts")
        hits = (me.get("challenges") or {}).get("enemyChampionImmobilizations")
        if not casts or hits is None:
            continue
        minutes = game["duration"] / 60 if game["duration"] else None
        out.append(dict(game, casts=casts, hits=hits,
                        per_min=casts / minutes if minutes else None))
    return out


def ratio(rows):
    """Pooled hits / casts, so long games weigh by how many Charms they had."""
    casts = sum(r["casts"] for r in rows)
    return sum(r["hits"] for r in rows) / casts if casts else None


def pct(value, width=6):
    return "-".rjust(width) if value is None else f"{100 * value:{width - 1}.0f}%"


def rounded(value, places=4):
    return None if value is None else round(value, places)


def charm_group(rows):
    """{n, rate, ci} for a set of games: pooled rate with a per-game bootstrap CI.

    The rate is an end-of-game estimate (immobilizations / E casts), not a hit
    log, and a gap between two groups is not evidence of what caused it.
    """
    ci = bootstrap_ci(rows, ratio)
    return {"n": len(rows), "rate": rounded(ratio(rows)),
            "ci": [rounded(v) for v in ci] if ci else None}


def charm_report(conn):
    """The /api/charm payload: per game (newest first), W/L split, by opponent."""
    rows = charm_records(conn)
    games = [{"match_id": r["match_id"], "start": r["date"], "opponent": r["opp"],
              "win": bool(r["win"]), "casts": r["casts"], "hits": r["hits"],
              "rate": rounded(r["hits"] / r["casts"]), "per_min": rounded(r["per_min"], 3)}
             for r in reversed(rows)]
    groups = defaultdict(list)
    for r in rows:
        groups[r["opp"]].append(r)
    by_opp = [dict(opponent=opp, **charm_group(g))
              for opp, g in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0] or ""))]
    return {"games": games,
            "summary": {"all": charm_group(rows),
                        "wins": charm_group([r for r in rows if r["win"]]),
                        "losses": charm_group([r for r in rows if not r["win"]])},
            "by_opponent": by_opp,
            "method": ci_method()}


def charm_line(label, group):
    return f"  {label:<18}{group['n']:>3}  {pct(group['rate'])}{pct_range(group['ci'], 12)}"


def section_charm(conn, min_games=1):
    rows = charm_records(conn)
    print("\nCharm estimate - enemy immobilizations per E cast (Ahri)")
    if not rows:
        print("  No Ahri games with spell3Casts / challenge stats stored.")
        return
    print(f"{'date':<11}{'matchup':<24}{'res':<5}{'E':>5}{'imm':>5}{'rate':>7}{'E/min':>7}")
    print("-" * 64)
    for r in reversed(rows):
        date = (datetime.fromtimestamp(r["date"] / 1000).strftime("%Y-%m-%d")
                if r["date"] else "?")
        print(f"{date:<11}{'Ahri vs ' + (r['opp'] or '?'):<24}{'W' if r['win'] else 'L':<5}"
              f"{r['casts']:>5}{r['hits']:>5}{pct(r['hits'] / r['casts']):>7}"
              f"{num(r['per_min'], 7, 2)}")

    print("-" * 64)
    ci_head = f"{CI_LEVEL:.0%} CI"
    print(f"  {'':<18}{'n':>3}  {'rate':>6}{ci_head:>12}")
    print(charm_line("All games", charm_group(rows))
          + f"   ({sum(r['hits'] for r in rows)}/{sum(r['casts'] for r in rows)})")
    print(charm_line("Wins", charm_group([r for r in rows if r["win"]])))
    print(charm_line("Losses", charm_group([r for r in rows if not r["win"]])))
    if len(rows) >= 6:
        half = len(rows) // 2
        print(charm_line("Older half", charm_group(rows[:half])))
        print(charm_line("Newer half", charm_group(rows[half:])))

    groups = defaultdict(list)
    for r in rows:
        groups[r["opp"] or "?"].append(r)
    shown = [(opp, g) for opp, g in groups.items() if len(g) >= min_games]
    if shown:
        print("\n  By opponent")
        for opp, g in sorted(shown, key=lambda kv: (-len(kv[1]), kv[0])):
            print(charm_line(opp, charm_group(g)))
    print("\n  An estimate from end-of-game totals; win/loss gaps do not show cause.")
    print(f"  CI: {CI_LEVEL:.0%} bootstrap over games ({CI_ITERATIONS} resamples); "
          f"'-' below {CI_MIN_GAMES} games.")


# ---------------------------------------------------------------- profile

def _challenge(name):
    return lambda p, m: (p.get("challenges") or {}).get(name)


# (key, label, getter(participant, duration_s), CLI format, unit) for the habit table.
# unit: "ratio" is 0-1 (shown as %), "per_10m" per 10 game minutes, "per_min" per minute.
PROFILE_STATS = [
    ("deaths_per_10m", "deaths/10m",
     lambda p, m: 600 * p.get("deaths", 0) / m if m else None, 1, "per_10m"),
    ("kill_participation", "kp", _challenge("killParticipation"), "pct", "ratio"),
    ("damage_share", "dmg share", _challenge("teamDamagePercentage"), "pct", "ratio"),
    ("damage_taken_share", "taken share", _challenge("damageTakenOnTeamPercentage"),
     "pct", "ratio"),
    ("vision_per_min", "vision/m", _challenge("visionScorePerMinute"), 2, "per_min"),
    ("time_dead_share", "dead %",
     lambda p, m: p.get("totalTimeSpentDead", 0) / m if m else None, "pct", "ratio"),
]


def profile_report(conn):
    """The /api/profile payload: per-champion mean of each habit with a per-game CI.

    Each value is a plain mean over the games that have the stat. The CI shows
    how much it could move with other games like these; it does not make roles
    comparable (a support's vision is not a mid's) and does not explain wins.
    """
    groups = defaultdict(list)
    for game, me in my_stats(conn):
        groups[game["champ"]].append((game, me))
    champions = []
    for champ, rows in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        stats = []
        for key, label, get, _fmt, unit in PROFILE_STATS:
            values = [v for v in (get(me, g["duration"]) for g, me in rows) if v is not None]
            ci = bootstrap_ci(values, mean)
            stats.append({"key": key, "label": label, "value": rounded(mean(values)),
                          "ci": [rounded(v) for v in ci] if ci else None,
                          "unit": unit, "n": len(values)})
        champions.append({"champion": champ, "n": len(rows),
                          "wins": sum(1 for g, _ in rows if g["win"]), "stats": stats})
    return {"champions": champions, "method": ci_method()}


def section_profile(conn, min_games=3):
    """End-of-game habits per champion you play, e.g. Ahri next to Lulu."""
    print(f"\nProfile - end-of-game habits per champion (min {min_games} games)")
    print(f"{'champion':<14}{'n':>3}{'W-L':>7}" + "".join(f"{s[1]:>12}" for s in PROFILE_STATS))
    print("-" * (24 + 12 * len(PROFILE_STATS)))
    ci_head = f"{CI_LEVEL:.0%} CI"
    for champ in profile_report(conn)["champions"]:
        if champ["n"] < min_games:
            continue
        cells, ranges = [], []
        for spec, stat in zip(PROFILE_STATS, champ["stats"]):
            fmt = spec[3]
            if fmt == "pct":
                cells.append(pct(stat["value"], 12))
                ranges.append(pct_range(stat["ci"], 12))
            else:
                cells.append(num(stat["value"], 12, fmt))
                ranges.append(num_range(stat["ci"], 12, fmt))
        n, wins = champ["n"], champ["wins"]
        print(f"{champ['champion']:<14}{n:>3}{f'{wins}-{n - wins}':>7}" + "".join(cells))
        print(f"{'':<14}{ci_head:>10}" + "".join(ranges))
    print("\n  Different roles have different norms; compare habits, not raw numbers.")
    print(f"  CI: {CI_LEVEL:.0%} bootstrap over games ({CI_ITERATIONS} resamples).")


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("section", nargs="?", default="all",
                    choices=["all", "matchups", "backs", "roams", "deaths",
                             "charm", "profile"])
    ap.add_argument("--db", default="league.db")
    ap.add_argument("--champions", default=",".join(DEFAULT_CHAMPIONS))
    ap.add_argument("--minute", type=int, default=10,
                    help="Lane snapshot minute for the matchup table (default: 10)")
    ap.add_argument("--matchup", help="Only games against opponents matching this name")
    ap.add_argument("--min-games", type=int, default=1,
                    help="Hide matchups you have played fewer than N times")
    args = ap.parse_args()

    champions = [c.strip() for c in args.champions.split(",") if c.strip()]
    conn = open_db(args.db)
    # Charm and profile read end-of-game stats, so they work without timelines.
    if args.section == "charm":
        section_charm(conn, args.min_games)
    if args.section == "profile":
        section_profile(conn, max(args.min_games, 3))
    if args.section in ("charm", "profile"):
        print()
        conn.close()
        return

    records = collect(conn, champions, args.minute, args.matchup)
    if not records:
        sys.exit(f"No games with timelines in {args.db} for "
                 f"{', '.join(champions)}. Run fetch_matches.py first.")

    if args.section in ("all", "matchups"):
        section_matchups(records, args.minute, args.min_games)
    if args.section in ("all", "backs"):
        section_backs(records)
    if args.section in ("all", "roams"):
        section_roams(records)
    if args.section in ("all", "deaths"):
        section_deaths(conn, records)
    if args.section == "all":
        section_charm(conn, args.min_games)
        section_profile(conn, max(args.min_games, 3))
    print()
    conn.close()


if __name__ == "__main__":
    main()
