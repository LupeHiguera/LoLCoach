"""Riot timeline facts known at one decision time, for `coach.harness prepare`.

Champion-named, whitelisted facts only: no names, PUUIDs or raw JSON. Nothing after
the decision is read. Gold, CS and map positions come from once-a-minute snapshots and
can be up to a minute old. None of it says what the player could see on screen.
"""
import json

WARD_WINDOW_MS = 120_000
MAX_OBJECTIVES = 12
SKILLS = {1: 'Q', 2: 'W', 3: 'E', 4: 'R'}
SIDES = {100: 'blue', 200: 'red'}
MONSTERS = dict(DRAGON='dragon', BARON_NASHOR='Baron Nashor', RIFTHERALD='Rift Herald',
                HORDE='Voidgrub', ATAKHAN='Atakhan')
DRAGONS = dict(WATER_DRAGON='Ocean dragon', FIRE_DRAGON='Infernal dragon', EARTH_DRAGON='Mountain dragon',
               AIR_DRAGON='Cloud dragon', HEXTECH_DRAGON='Hextech dragon',
               CHEMTECH_DRAGON='Chemtech dragon', ELDER_DRAGON='Elder dragon')
LANES = dict(TOP_LANE='top', MID_LANE='mid', BOT_LANE='bottom')
TOWERS = dict(OUTER_TURRET='outer turret', INNER_TURRET='inner turret',
              BASE_TURRET='inhibitor turret', NEXUS_TURRET='nexus turret')
NOTE = ("Riot timeline facts, not player view. Level and ability ranks are exact at decision_ms. "
        "Gold, CS and positions are from the last once-a-minute snapshot (sampled_ms). "
        "Map coordinates run from about 0 to 15000, starting at the blue-side base corner. "
        "Not included: items, summoner spells, cooldowns, health, mana and ward locations.")


def clock(ms):
    return f'{ms // 60000}:{ms // 1000 % 60:02d}'


def label(value, names):
    return names.get(value) or str(value or 'unknown').replace('_', ' ').lower()


def fact(fact_id, game_ms, kind, statement):
    return dict(id=fact_id, game_ms=game_ms, source='riot_timeline',
                visibility='global_event_not_player_view', kind=kind, statement=statement)


def roster(conn, match_id):
    """My participant ID and champion, team and role per participant; no identifiers."""
    row = conn.execute("SELECT my_participant_id FROM matches WHERE match_id=?", (match_id,)).fetchone()
    if row is None:
        raise ValueError('Match not found in the match database')
    players = {pid: dict(champion=champion, team=team, role=role) for pid, champion, team, role in conn.execute(
        "SELECT participant_id, champion, team_id, position FROM participants WHERE match_id=?", (match_id,))}
    if row[0] not in players:
        raise ValueError('Match has no participant row for the player')
    return row[0], players


def events_before(conn, match_id, decision_ms, *types):
    """Whitelisted fields are read from each event; the raw JSON never leaves this module."""
    marks = ','.join('?' * len(types))
    return [json.loads(raw) for (raw,) in conn.execute(
        f"""SELECT raw_json FROM events WHERE match_id=? AND timestamp_ms<=? AND type IN ({marks})
            ORDER BY timestamp_ms, id""", (match_id, decision_ms, *types))]


def player_facts(conn, match_id, decision_ms, me, players, sampled_ms):
    """Exact level and ability ranks; gold, CS and position from the snapshot."""
    levels, ranks = {}, {}
    for event in events_before(conn, match_id, decision_ms, 'LEVEL_UP', 'SKILL_LEVEL_UP'):
        pid = event.get('participantId')
        if event['type'] == 'LEVEL_UP':
            levels[pid] = max(levels.get(pid, 1), event.get('level') or 1)
        elif event.get('levelUpType') == 'NORMAL' and event.get('skillSlot') in SKILLS:
            slot = SKILLS[event['skillSlot']]
            ranks.setdefault(pid, dict.fromkeys(SKILLS.values(), 0))[slot] += 1
    snapshot = {}
    if sampled_ms is not None:
        snapshot = {r[0]: r[1:] for r in conn.execute("""SELECT participant_id, total_gold,
            minions, jungle_minions, x, y FROM frames WHERE match_id=? AND timestamp_ms=?""",
            (match_id, sampled_ms))}
    my_team = players[me]['team']
    order = sorted(players, key=lambda pid: (pid != me, players[pid]['team'] != my_team, pid))
    facts = []
    for pid in order:
        player = players[pid]
        who = f"{'Ally' if player['team'] == my_team else 'Enemy'} {player['champion']}"
        detail = ', '.join(filter(None, [player['role'], 'you' if pid == me else None]))
        skills = ' '.join(f'{k}{v}' for k, v in ranks.get(pid, dict.fromkeys(SKILLS.values(), 0)).items())
        text = f"{who}{f' ({detail})' if detail else ''}: level {levels.get(pid, 1)}, ability ranks {skills}."
        if pid in snapshot:
            gold, minions, jungle, x, y = snapshot[pid]
            text += (f" At {clock(sampled_ms)}: {gold or 0:,} total gold, {(minions or 0) + (jungle or 0)} CS"
                     + (f', map position x={x} y={y}.' if x is not None and y is not None else '.'))
        facts.append(fact(f'riot-player-{pid}', sampled_ms if sampled_ms is not None else 0, 'player', text))
    return facts


def objective_facts(events, my_team):
    """Most recent elite monsters, buildings and souls, each with its time and side."""
    def side(team):
        return 'ally' if team == my_team else 'enemy'
    facts = []
    for index, event in enumerate(events):
        kind, ms = event['type'], event['timestamp']
        if kind == 'ELITE_MONSTER_KILL':
            monster = (label(event.get('monsterSubType'), DRAGONS) if event.get('monsterType') == 'DRAGON'
                       else label(event.get('monsterType'), MONSTERS))
            text = f"{side(event.get('killerTeamId')).capitalize()} team took {monster} at {clock(ms)}."
        elif kind == 'BUILDING_KILL':
            building = ('inhibitor' if event.get('buildingType') == 'INHIBITOR_BUILDING'
                        else label(event.get('towerType'), TOWERS))
            text = (f"{side(event.get('teamId')).capitalize()} {label(event.get('laneType'), LANES)} "
                    f"{building} destroyed at {clock(ms)}.")
        elif kind == 'DRAGON_SOUL_GIVEN' and event.get('teamId') in SIDES:
            text = f"{side(event['teamId']).capitalize()} team gained {event.get('name', 'a')} dragon soul at {clock(ms)}."
        elif kind == 'DRAGON_SOUL_GIVEN':
            # teamId 0 marks the map changing to the soul element, not a team gaining the soul.
            text = f"Map changed to {event.get('name', 'unknown')} dragon soul element at {clock(ms)}."
        else:
            continue
        facts.append(fact(f'riot-objective-{ms}-{index}', ms, 'objective', text))
    return facts[-MAX_OBJECTIVES:]


def score_fact(conn, match_id, decision_ms, my_team, players, events):
    """Running totals, so objectives dropped by the cap still count."""
    kills = {'ally': 0, 'enemy': 0}
    for (victim,) in conn.execute("""SELECT victim_id FROM events WHERE match_id=?
            AND type='CHAMPION_KILL' AND timestamp_ms<=?""", (match_id, decision_ms)):
        if victim in players:
            kills['enemy' if players[victim]['team'] == my_team else 'ally'] += 1
    taken = dict(towers={'ally': 0, 'enemy': 0}, plates={'ally': 0, 'enemy': 0}, dragons={'ally': 0, 'enemy': 0})
    for event in events:
        if event['type'] in ('BUILDING_KILL', 'TURRET_PLATE_DESTROYED') and event.get('teamId') in SIDES:
            key = 'towers' if event['type'] == 'BUILDING_KILL' else 'plates'
            taken[key]['enemy' if event['teamId'] == my_team else 'ally'] += 1
        elif event['type'] == 'ELITE_MONSTER_KILL' and event.get('monsterType') == 'DRAGON':
            taken['dragons']['ally' if event.get('killerTeamId') == my_team else 'enemy'] += 1
    totals = '; '.join(f"{key} by allies {value['ally']}, by enemies {value['enemy']}" for key, value in
                       {'champion kills': kills, 'structures destroyed': taken['towers'],
                        'plates taken': taken['plates'], 'dragons taken': taken['dragons']}.items())
    return fact('riot-score', decision_ms, 'score', f'Totals at {clock(decision_ms)}: {totals}.')


def ward_fact(conn, match_id, decision_ms, my_team, players):
    """Counts only: the Riot timeline has no ward positions."""
    start = max(0, decision_ms - WARD_WINDOW_MS)
    counts = {('ally', 'placed'): 0, ('ally', 'cleared'): 0, ('enemy', 'placed'): 0, ('enemy', 'cleared'): 0}
    for event in events_before(conn, match_id, decision_ms, 'WARD_PLACED', 'WARD_KILL'):
        who = event.get('creatorId') if event['type'] == 'WARD_PLACED' else event.get('killerId')
        if event['timestamp'] < start or who not in players or event.get('wardType') == 'UNDEFINED':
            continue
        side = 'ally' if players[who]['team'] == my_team else 'enemy'
        counts[side, 'placed' if event['type'] == 'WARD_PLACED' else 'cleared'] += 1
    return fact('riot-wards', decision_ms, 'wards',
                f"From {clock(start)} to {clock(decision_ms)}: allies placed {counts['ally', 'placed']} "
                f"wards and cleared {counts['ally', 'cleared']}; enemies placed {counts['enemy', 'placed']} "
                f"and cleared {counts['enemy', 'cleared']}. Ward locations are not recorded.")


def facts_at(conn, match_id, decision_ms):
    """State block for one moment; every fact is at or before decision_ms."""
    if type(decision_ms) is not int or decision_ms < 0:
        raise ValueError('Decision must be a nonnegative integer ms')
    me, players = roster(conn, match_id)
    if conn.execute("SELECT 1 FROM timelines WHERE match_id=?", (match_id,)).fetchone() is None:
        raise ValueError('This match has no timeline; fetch it, or use --no-timeline')
    my_team = players[me]['team']
    sampled_ms = conn.execute("SELECT MAX(timestamp_ms) FROM frames WHERE match_id=? AND timestamp_ms<=?",
                              (match_id, decision_ms)).fetchone()[0]
    events = events_before(conn, match_id, decision_ms, 'ELITE_MONSTER_KILL', 'BUILDING_KILL',
                           'DRAGON_SOUL_GIVEN', 'TURRET_PLATE_DESTROYED')
    facts = (player_facts(conn, match_id, decision_ms, me, players, sampled_ms)
             + objective_facts(events, my_team)
             + [score_fact(conn, match_id, decision_ms, my_team, players, events),
                ward_fact(conn, match_id, decision_ms, my_team, players)])
    return dict(as_of_ms=decision_ms, sampled_ms=sampled_ms, my_side=SIDES.get(my_team),
                note=NOTE, facts=facts)
