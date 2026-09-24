import io
import sqlite3
import unittest
from contextlib import redirect_stdout

from analyze import charm_records, my_stats, ratio, section_charm, section_profile
from fetch_matches import SCHEMA, store_match

ME = "me-puuid"


def match(match_id, champ, win, e_casts=None, immobilizations=None, start=0, duration=1800):
    me = dict(participantId=1, puuid=ME, teamId=100, championName=champ, win=win,
              teamPosition="MIDDLE", kills=1, deaths=3, assists=2, totalTimeSpentDead=90,
              challenges={"killParticipation": 0.5})
    if e_casts is not None:
        me["spell3Casts"] = e_casts
    if immobilizations is not None:
        me["challenges"]["enemyChampionImmobilizations"] = immobilizations
    opp = dict(participantId=6, puuid="opp", teamId=200, championName="Syndra", win=not win,
               teamPosition="MIDDLE", kills=0, deaths=1, assists=0)
    return {"metadata": {"matchId": match_id},
            "info": {"gameStartTimestamp": start, "gameDuration": duration, "queueId": 420,
                     "gameVersion": "15.18.1", "participants": [me, opp]}}


class CharmTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def add(self, *args, **kwargs):
        store_match(self.conn, match(*args, **kwargs), ME)

    def test_rate_is_pooled_across_games(self):
        self.add("A", "Ahri", True, e_casts=10, immobilizations=8, start=1)
        self.add("B", "Ahri", False, e_casts=30, immobilizations=12, start=2)
        rows = charm_records(self.conn)
        self.assertEqual([r["match_id"] for r in rows], ["A", "B"])
        self.assertAlmostEqual(ratio(rows), 20 / 40)

    def test_other_champions_and_missing_stats_are_skipped(self):
        self.add("A", "Lulu", True, e_casts=10, immobilizations=8)
        self.add("B", "Ahri", True)  # older match JSON without the fields
        self.add("C", "Ahri", True, e_casts=0, immobilizations=0)
        self.assertEqual(charm_records(self.conn), [])

    def test_empty_ratio_is_none(self):
        self.assertIsNone(ratio([]))

    def test_sections_print_without_timelines(self):
        self.add("A", "Ahri", True, e_casts=10, immobilizations=5)
        for i in range(3):
            self.add(f"L{i}", "Lulu", True, start=i)
        out = io.StringIO()
        with redirect_stdout(out):
            section_charm(self.conn)
            section_profile(self.conn)
        text = out.getvalue()
        self.assertIn("(5/10)", text)
        self.assertIn("Lulu", text)
        self.assertNotIn("Ahri   ", text.split("Profile")[1])  # 1 game < min 3

    def test_my_stats_finds_my_participant(self):
        self.add("A", "Ahri", True, e_casts=4, immobilizations=1)
        (game, me), = my_stats(self.conn)
        self.assertEqual((game["champ"], me["puuid"]), ("Ahri", ME))


if __name__ == "__main__":
    unittest.main()
