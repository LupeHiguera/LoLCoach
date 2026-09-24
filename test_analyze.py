import io
import sqlite3
import unittest
from contextlib import redirect_stdout

from analyze import (CI_MIN_GAMES, PROFILE_STATS, bootstrap_ci, charm_records, charm_report,
                     mean, my_stats, profile_report, ratio, section_charm, section_profile)
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


class BootstrapTests(unittest.TestCase):
    def test_deterministic_and_brackets_the_estimate(self):
        values = [0.3, 0.5, 0.4, 0.6, 0.55, 0.45]
        ci = bootstrap_ci(values, mean)
        self.assertEqual(ci, bootstrap_ci(values, mean))
        self.assertLessEqual(ci[0], mean(values))
        self.assertGreaterEqual(ci[1], mean(values))
        self.assertGreaterEqual(ci[0], min(values))
        self.assertLessEqual(ci[1], max(values))

    def test_too_few_games_gives_no_interval(self):
        self.assertIsNone(bootstrap_ci([0.5] * (CI_MIN_GAMES - 1), mean))
        self.assertIsNone(bootstrap_ci([], mean))

    def test_identical_games_give_zero_width(self):
        self.assertEqual(bootstrap_ci([0.5, 0.5, 0.5], mean), [0.5, 0.5])

    def test_resamples_games_not_casts(self):
        rows = [dict(casts=10, hits=10), dict(casts=10, hits=0)]
        lo, hi = bootstrap_ci(rows, ratio)
        self.assertEqual((lo, hi), (0.0, 1.0))


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(SCHEMA)

    def add(self, *args, **kwargs):
        store_match(self.conn, match(*args, **kwargs), ME)

    def test_charm_report_shape(self):
        self.add("A", "Ahri", True, e_casts=10, immobilizations=6, start=1)
        self.add("B", "Ahri", False, e_casts=20, immobilizations=8, start=2)
        self.add("C", "Ahri", False, e_casts=10, immobilizations=3, start=3)
        report = charm_report(self.conn)
        self.assertEqual([g["match_id"] for g in report["games"]], ["C", "B", "A"])
        game = report["games"][-1]
        self.assertEqual(set(game), {"match_id", "start", "opponent", "win", "casts", "hits",
                                     "rate", "per_min"})
        self.assertIs(game["win"], True)
        self.assertEqual((game["opponent"], game["rate"], game["per_min"]), ("Syndra", 0.6, 0.333))
        summary = report["summary"]
        self.assertEqual(summary["all"]["n"], 3)
        self.assertAlmostEqual(summary["all"]["rate"], round(17 / 40, 4))
        lo, hi = summary["all"]["ci"]
        self.assertLessEqual(lo, summary["all"]["rate"])
        self.assertGreaterEqual(hi, summary["all"]["rate"])
        self.assertEqual((summary["wins"]["n"], summary["wins"]["ci"]), (1, None))
        self.assertEqual(summary["losses"]["n"], 2)
        self.assertEqual(report["by_opponent"], [dict(opponent="Syndra", **summary["all"])])

    def test_charm_report_empty(self):
        report = charm_report(self.conn)
        self.assertEqual(report["games"], [])
        self.assertEqual(report["summary"]["all"], {"n": 0, "rate": None, "ci": None})

    def test_profile_report_shape(self):
        for i in range(3):
            self.add(f"L{i}", "Lulu", i != 0, start=i, duration=1200 + 60 * i)
        self.add("A", "Ahri", True, e_casts=4, immobilizations=1)
        champs = profile_report(self.conn)["champions"]
        self.assertEqual([(c["champion"], c["n"], c["wins"]) for c in champs],
                         [("Lulu", 3, 2), ("Ahri", 1, 1)])
        stats = {s["key"]: s for s in champs[0]["stats"]}
        self.assertEqual(list(stats), [spec[0] for spec in PROFILE_STATS])
        deaths = stats["deaths_per_10m"]
        self.assertEqual((deaths["unit"], deaths["n"]), ("per_10m", 3))
        self.assertAlmostEqual(deaths["value"], round(mean([1.5, 1800 / 1260, 1800 / 1320]), 4))
        self.assertIsNotNone(deaths["ci"])
        self.assertEqual(stats["kill_participation"]["ci"], [0.5, 0.5])
        # a missing challenge is null with n=0, never zero
        self.assertEqual((stats["vision_per_min"]["value"], stats["vision_per_min"]["n"]), (None, 0))
        self.assertIsNone(champs[1]["stats"][0]["ci"])


if __name__ == "__main__":
    unittest.main()
