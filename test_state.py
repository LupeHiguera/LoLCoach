import json
import sqlite3
import unittest

import fetch_matches
from coach import contracts, harness, state

MATCH = 'private-match-id'
PLAYERS = {1: ('Ahri', 100, 'MIDDLE'), 2: ('LeeSin', 100, 'JUNGLE'),
           6: ('Akali', 200, 'MIDDLE'), 7: ('Vi', 200, 'JUNGLE')}


def snapshot(gold, x):
    return {str(pid): dict(totalGold=gold + pid, currentGold=0, xp=0, level=1, minionsKilled=10 * pid,
                           jungleMinionsKilled=pid, position=dict(x=x, y=x + 1)) for pid in PLAYERS}


def build_match(conn, match_id=MATCH):
    """Synthetic match: the player (Ahri, blue) dies at 1:58; later events must stay out."""
    conn.executescript(fetch_matches.SCHEMA)
    conn.execute("INSERT INTO matches (match_id, my_participant_id, my_champion, raw_json) VALUES (?,1,'Ahri',?)",
                 (match_id, '{"puuid": "private-puuid"}'))
    for pid, (champion, team, role) in PLAYERS.items():
        conn.execute("INSERT INTO participants (match_id, participant_id, puuid, champion, team_id, position)"
                     " VALUES (?,?,?,?,?,?)", (match_id, pid, f'private-puuid-{pid}', champion, team, role))
    frames = [
        dict(timestamp=0, participantFrames=snapshot(500, 100), events=[
            dict(type='SKILL_LEVEL_UP', timestamp=1000, participantId=1, skillSlot=1, levelUpType='NORMAL')]),
        dict(timestamp=60000, participantFrames=snapshot(1500, 6000), events=[
            dict(type='LEVEL_UP', timestamp=70000, participantId=1, level=2),
            dict(type='SKILL_LEVEL_UP', timestamp=70000, participantId=1, skillSlot=3, levelUpType='NORMAL'),
            dict(type='WARD_PLACED', timestamp=100000, creatorId=7, wardType='YELLOW_TRINKET'),
            dict(type='WARD_PLACED', timestamp=101000, creatorId=0, wardType='UNDEFINED'),
            dict(type='ELITE_MONSTER_KILL', timestamp=110000, killerId=7, killerTeamId=200,
                 monsterType='DRAGON', monsterSubType='WATER_DRAGON', position=dict(x=9000, y=4000)),
            dict(type='TURRET_PLATE_DESTROYED', timestamp=115000, killerId=1, teamId=200, laneType='MID_LANE'),
            dict(type='CHAMPION_KILL', timestamp=118000, killerId=6, victimId=1, killerName='PrivateName',
                 assistingParticipantIds=[7], position=dict(x=7000, y=7000))]),
        dict(timestamp=120000, participantFrames=snapshot(2500, 7000), events=[
            dict(type='LEVEL_UP', timestamp=150000, participantId=1, level=3),
            dict(type='BUILDING_KILL', timestamp=160000, killerId=6, teamId=100, buildingType='TOWER_BUILDING',
                 laneType='MID_LANE', towerType='OUTER_TURRET')]),
        dict(timestamp=180000, participantFrames=snapshot(9999, 9999), events=[]),
    ]
    fetch_matches.store_timeline(conn, match_id, dict(info=dict(frameInterval=60000, frames=frames)))


def statements(facts):
    return {f['id']: f['statement'] for f in facts['facts']}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        build_match(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_facts_are_exact_known_at_decision_and_whitelisted(self):
        facts = state.facts_at(self.conn, MATCH, 130000)
        text = statements(facts)
        self.assertEqual((facts['as_of_ms'], facts['sampled_ms'], facts['my_side']), (130000, 120000, 'blue'))
        self.assertEqual(facts['facts'][0]['id'], 'riot-player-1')
        self.assertEqual(text['riot-player-1'], 'Ally Ahri (MIDDLE, you): level 2, ability ranks Q1 W0 E1 R0. '
                                                'At 2:00: 2,501 total gold, 11 CS, map position x=7000 y=7001.')
        self.assertTrue(text['riot-player-7'].startswith('Enemy Vi (JUNGLE): level 1, ability ranks Q0 W0 E0 R0.'))
        self.assertEqual([f['statement'] for f in facts['facts'] if f['kind'] == 'objective'],
                         ['Enemy team took Ocean dragon at 1:50.'])
        self.assertEqual(text['riot-score'], 'Totals at 2:10: champion kills by allies 0, by enemies 1; '
                                             'structures destroyed by allies 0, by enemies 0; plates taken '
                                             'by allies 1, by enemies 0; dragons taken by allies 0, by enemies 1.')
        self.assertIn('enemies placed 1 and cleared 0', text['riot-wards'])
        self.assertTrue(all(f['game_ms'] <= 130000 for f in facts['facts']))
        dumped = json.dumps(facts)
        for private in ('private', 'PrivateName', MATCH, '9999', 'level 3', 'turret destroyed'):
            self.assertNotIn(private, dumped)

    def test_later_decision_sees_later_facts(self):
        text = statements(state.facts_at(self.conn, MATCH, 170000))
        self.assertIn('level 3', text['riot-player-1'])
        self.assertIn('Ally mid outer turret destroyed at 2:40.', text.values())
        self.assertIn('structures destroyed by allies 0, by enemies 1', text['riot-score'])

    def test_soul_element_reveal_is_not_a_team_gaining_soul(self):
        for team, expected in ((0, 'Map changed to Chemtech dragon soul element at 0:05.'),
                               (200, 'Enemy team gained Chemtech dragon soul at 0:05.')):
            self.conn.execute("DELETE FROM events WHERE type='DRAGON_SOUL_GIVEN'")
            self.conn.execute("INSERT INTO events (match_id, timestamp_ms, type, raw_json) VALUES (?,5000,?,?)",
                              (MATCH, 'DRAGON_SOUL_GIVEN', json.dumps(dict(
                                  type='DRAGON_SOUL_GIVEN', timestamp=5000, teamId=team, name='Chemtech'))))
            with self.subTest(team=team):
                self.assertIn(expected, statements(state.facts_at(self.conn, MATCH, 130000)).values())

    def test_objectives_are_capped_but_totals_still_count(self):
        for i in range(state.MAX_OBJECTIVES + 3):
            self.conn.execute("INSERT INTO events (match_id, timestamp_ms, type, raw_json) VALUES (?,?,?,?)",
                              (MATCH, 2000 + i, 'ELITE_MONSTER_KILL', json.dumps(dict(
                                  type='ELITE_MONSTER_KILL', timestamp=2000 + i, killerTeamId=100,
                                  monsterType='HORDE'))))
        facts = state.facts_at(self.conn, MATCH, 130000)
        objectives = [f for f in facts['facts'] if f['kind'] == 'objective']
        self.assertEqual(len(objectives), state.MAX_OBJECTIVES)
        self.assertEqual(objectives[-1]['statement'], 'Enemy team took Ocean dragon at 1:50.')
        self.assertLessEqual(len(facts['facts']), contracts.MAX_FACTS)

    def test_missing_timeline_or_player_is_an_error(self):
        self.conn.execute("DELETE FROM timelines")
        with self.assertRaisesRegex(ValueError, 'no timeline'):
            state.facts_at(self.conn, MATCH, 130000)
        with self.assertRaisesRegex(ValueError, 'not found'):
            state.facts_at(self.conn, 'other-match', 130000)
        for bad in (-1, True, 1.5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                state.facts_at(self.conn, MATCH, bad)


class PacketStateTests(unittest.TestCase):
    def setUp(self):
        from test_harness import manifest
        conn = sqlite3.connect(':memory:')
        build_match(conn)
        self.facts = state.facts_at(conn, MATCH, 295000)
        conn.close()
        self.packet = harness.make_packet(manifest(), 'death-300000', 295000, 55000, 3, state=self.facts)[0]

    def observations(self):
        frame = self.packet['frames'][-1]
        return dict(schema_version=1, moment_id=self.packet['moment_id'], unknowns=[],
                    observations=[dict(id='obs-1', game_ms=frame['game_ms'], statement='Ahri is visible.',
                                       source='local_vision', verification='model_observed',
                                       evidence_refs=[frame['id']])])

    def test_review_can_cite_state_and_observe_never_sees_it(self):
        self.assertEqual(self.packet['schema_version'], 2)
        review = dict(schema_version=1, moment_id=self.packet['moment_id'], assessment='needs_more_evidence',
                      claims=[dict(statement='Enemy jungler was level 1.', kind='observation',
                                   evidence_refs=['riot-player-7'])],
                      alternative=None, practice_focus=None, missing_evidence=['Minimap is unread.'])
        contracts.validate_review(review, self.packet, self.observations())
        observe = harness.build_request('observe', self.packet, 'vision', preview=True)
        self.assertIsNone(json.loads(observe['messages'][1]['content'][0]['text'])['packet']['state'])
        coach = harness.build_request('review', self.packet, 'coach', self.observations())
        self.assertIn('riot-player-7', coach['messages'][1]['content'][0]['text'])

    def test_future_or_misdated_state_rejected(self):
        for change in ('fact', 'sampled', 'as_of', 'collision', 'kind'):
            p = json.loads(json.dumps(self.packet))
            if change == 'fact':
                p['state']['facts'][0]['game_ms'] = 296000
            elif change == 'sampled':
                p['state']['sampled_ms'] = 296000
            elif change == 'as_of':
                p['state']['as_of_ms'] = 290000
            elif change == 'collision':
                p['state']['facts'][0]['id'] = p['frames'][0]['id']
            else:
                p['state']['facts'][0]['kind'] = 'diagnosis'
            with self.subTest(change=change), self.assertRaises(ValueError):
                contracts.validate_packet(p)

    def test_observation_id_cannot_shadow_a_fact(self):
        o = self.observations()
        o['observations'][0]['id'] = 'riot-score'
        with self.assertRaisesRegex(ValueError, 'collides'):
            contracts.validate_observations(o, self.packet)

    def test_v1_packets_and_missing_state_stay_valid(self):
        contracts.validate_packet(harness.read_json('coach/examples/packet.json'))
        contracts.validate_packet(dict(self.packet, state=None))
        with self.assertRaises(ValueError):
            contracts.validate_packet(dict(self.packet, schema_version=1))


if __name__ == '__main__':
    unittest.main()
