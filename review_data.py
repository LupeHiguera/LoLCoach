"""Local review storage and timeline evidence, independent of any UI or server."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import analyze
import recorder

REASONS = {"uncertain", "dead", "recalling", "fighting", "roaming", "pressured", "other"}
EVIDENCE = {"stats_only", "recording", "memory"}
# The next games after a focus is saved, not a rolling window and not a rate.
FOCUS_HORIZON = 5
FOCUS_METRICS = {"", "deaths_before"}


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
                    check_text TEXT, updated_at TEXT, metric TEXT,
                    before_ms INTEGER, limit_n INTEGER);
            """)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(focus)")}
            for name, decl in (("metric", "TEXT"), ("before_ms", "INTEGER"), ("limit_n", "INTEGER")):
                if name not in cols:
                    conn.execute(f"ALTER TABLE focus ADD COLUMN {name} {decl}")
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
            kills = kill_feed(conn, match_id, match['my_participant_id'])
            totals = score(conn, match_id, match['my_participant_id'], kills)
        moments = find_moments(frames, deaths, cadence)
        with closing(connect(self.notes_path)) as conn:
            reviews = [dict(r) for r in conn.execute("SELECT * FROM reviews WHERE match_id=? ORDER BY start_ms", (match_id,))]
        return dict(match=match, frames=frames, deaths=deaths, score=totals, kills=kills,
                    moments=moments, reviews=reviews,
                    cadence_ms=cadence, recording=self.recording(match_id))

    def link_recordings(self):
        """Link saved OBS recordings to matches fetched since (see recorder.link_recordings)."""
        with closing(connect(self.notes_path)) as notes, closing(connect(self.matches_path, True)) as league:
            return recorder.link_recordings(notes, league)

    def recording_path(self, match_id):
        """The stored recording's file if it still exists; no filename guessing."""
        with closing(connect(self.notes_path)) as conn:
            row = recorder.recording_for(conn, match_id)
        path = Path(row[0]) if row else None
        return path if path is not None and path.is_file() else None

    def recording(self, match_id):
        """{path, offset_s, status, url=None} for a recording; no HTTP server is assumed.

        offset_s is added to game time to get video time. It comes from the game
        clock read when OBS started, so it can be off by a second or so.
        """
        self.link_recordings()
        with closing(connect(self.notes_path)) as conn:
            row = recorder.recording_for(conn, match_id)
        if row is None:
            return None
        url = None  # No HTTP server is required by the storage layer.
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
        """The saved goal, plus a timeline count for games that started after it.

        `progress` is null when the check is only a note. A death count is how many
        timeline deaths fall before `before_ms`. It is not a cause and not a win rate.
        """
        with closing(connect(self.notes_path)) as conn:
            row = conn.execute("""SELECT goal, why_text, check_text, metric, before_ms,
                limit_n, updated_at FROM focus WHERE id=1""").fetchone()
        if row is None:
            return dict(goal='', why_text='', check_text='', metric=None, before_ms=None,
                        limit_n=None, updated_at=None, progress=None)
        saved = dict(row)
        metric = saved['metric'] or None
        return dict(goal=saved['goal'] or '', why_text=saved['why_text'] or '',
                    check_text=saved['check_text'] or '', metric=metric,
                    before_ms=saved['before_ms'], limit_n=saved['limit_n'],
                    updated_at=saved['updated_at'],
                    progress=self.focus_progress(saved) if metric else None)

    def focus_progress(self, saved):
        """First FOCUS_HORIZON Ahri/Zoe mid games that started after this focus."""
        since = iso_to_ms(saved['updated_at']) if saved.get('updated_at') else None
        if since is None or saved.get('metric') != 'deaths_before':
            return None
        before_ms, limit_n = saved['before_ms'], saved['limit_n']
        games = []
        with closing(connect(self.matches_path, True)) as conn:
            rows = conn.execute("""
                SELECT m.match_id, m.game_start_ms, m.my_champion, m.opp_champion, m.my_participant_id
                FROM matches m JOIN timelines t USING(match_id)
                WHERE m.my_champion IN ('Ahri', 'Zoe') AND m.my_position='MIDDLE'
                  AND m.game_start_ms > ?
                ORDER BY m.game_start_ms ASC
                LIMIT ?
            """, (since, FOCUS_HORIZON)).fetchall()
            for item in rows:
                deaths = conn.execute("""SELECT COUNT(*) FROM events
                    WHERE match_id=? AND type='CHAMPION_KILL' AND victim_id=? AND timestamp_ms < ?""",
                    (item['match_id'], item['my_participant_id'], before_ms)).fetchone()[0]
                label = clock_label(before_ms)
                games.append(dict(
                    match_id=item['match_id'], game_start_ms=item['game_start_ms'],
                    my_champion=item['my_champion'], opp_champion=item['opp_champion'],
                    deaths=deaths, met=deaths <= limit_n,
                    detail=f"{deaths} death{'' if deaths == 1 else 's'} before {label}"))
        met = sum(game['met'] for game in games)
        return dict(horizon=FOCUS_HORIZON, n=len(games), met=met, games=games,
                    note=(f"Timeline deaths before {clock_label(before_ms)} in Ahri or Zoe mid games "
                          f"that started after this focus was saved. Met means at most {limit_n}. "
                          f"Showing {len(games)} of the next {FOCUS_HORIZON}. "
                          "A count is a review prompt, not a cause."))

    def save_focus(self, body):
        values = [text_field(body, key, 1000) for key in ('goal', 'why_text', 'check_text')]
        if not values[0].strip():
            raise ValueError("Write a practice goal first.")
        metric = text_field(body, 'metric', 40)
        if metric not in FOCUS_METRICS:
            raise ValueError("Choose a timeline count or leave it blank.")
        before_ms = limit_n = None
        if metric == 'deaths_before':
            before_ms = body.get('before_ms', 600_000)
            limit_n = body.get('limit_n', 0)
            if type(before_ms) is not int or not 60_000 <= before_ms <= 1_200_000:
                raise ValueError("Count deaths before a time from 1:00 to 20:00.")
            if type(limit_n) is not int or not 0 <= limit_n <= 20:
                raise ValueError("The death limit must be a whole number from 0 to 20.")
        else:
            metric = None
        with closing(connect(self.notes_path)) as conn, conn:
            conn.execute("""INSERT OR REPLACE INTO focus
                (id, goal, why_text, check_text, updated_at, metric, before_ms, limit_n)
                VALUES (1,?,?,?,?,?,?,?)""", (*values, now(), metric, before_ms, limit_n))
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
        if evidence not in EVIDENCE:
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


def iso_to_ms(value):
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def clock_label(ms):
    """Game clock as M:SS. Used on the focus count, which is a threshold, not a diagnosis."""
    return f"{ms // 60000}:{(ms // 1000) % 60:02d}"


def kill_feed(conn, match_id, me):
    """Every champion kill in the timeline, from my team's point of view (API.md, `kills`).

    Champion names only. `side` says which team got the kill; `me` is my part in it.
    Kills credited to towers, minions or monsters have killer None.
    """
    players = {r[0]: (r[1], r[2]) for r in conn.execute(
        "SELECT participant_id, champion, team_id FROM participants WHERE match_id=?", (match_id,))}
    my_team = players.get(me, (None, None))[1]
    feed = []
    for time, killer, victim, assisting, x, y in conn.execute("""SELECT timestamp_ms, killer_id,
            victim_id, assisting_ids, x, y FROM events WHERE match_id=? AND type='CHAMPION_KILL'
            ORDER BY timestamp_ms, id""", (match_id,)):
        helpers = json.loads(assisting) if assisting else []
        victim_team = players.get(victim, (None, None))[1]
        side = None if victim_team is None or my_team is None else ('enemy' if victim_team == my_team else 'ally')
        role = 'kill' if killer == me else 'death' if victim == me else 'assist' if me in helpers else None
        feed.append(dict(time=time, side=side, killer=players.get(killer, (None,))[0],
                         victim=players.get(victim, (None,))[0], me=role, x=x, y=y,
                         assists=[players[h][0] for h in helpers if h in players]))
    return feed


def score(conn, match_id, me, feed):
    """My end-of-game K/D/A plus team kill totals counted from the timeline feed."""
    row = conn.execute("SELECT kills, deaths, assists FROM participants WHERE match_id=? AND participant_id=?",
                       (match_id, me)).fetchone()
    kda = dict(zip(('kills', 'deaths', 'assists'), row)) if row else dict(kills=None, deaths=None, assists=None)
    known = [k for k in feed if k['side']]
    return dict(kda, team_kills=sum(k['side'] == 'ally' for k in known) if known else None,
                enemy_kills=sum(k['side'] == 'enemy' for k in known) if known else None)


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
