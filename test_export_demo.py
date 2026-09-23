import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from export_demo import ScrubError, build, check_scrubbed, write
from fetch_matches import SCHEMA, store_match, store_timeline
from review_app import ReviewStore

ME = "my-puuid-0000-aaaa"
SECRETS = {ME, "enemy-puuid-1111-bbbb", "ally-puuid-2222-cccc", "MySecretName", "NA9",
           "EnemyPlayerName", "EUW7", "AllyPlayerName", "summoner-id-zzz", "NA1_7000000001",
           "NA1_7000000002", "NA1_7000000003", "MySecretName#NA9"}


def player(pid, puuid, name, tag, champ, team, win, challenges=None, **extra):
    return dict(participantId=pid, puuid=puuid, riotIdGameName=name, riotIdTagline=tag,
                summonerName=name, summonerId="summoner-id-zzz", championName=champ,
                teamId=team, win=win, teamPosition="MIDDLE" if pid in (1, 6) else "BOTTOM",
                kills=2, deaths=3, assists=4, totalTimeSpentDead=60,
                challenges=challenges or {"killParticipation": 0.5}, **extra)


def riot_match(match_id, start, win):
    parts = [player(1, ME, "MySecretName", "NA9", "Ahri", 100, win, spell3Casts=20,
                    challenges={"killParticipation": 0.5, "enemyChampionImmobilizations": 9}),
             player(2, "ally-puuid-2222-cccc", "AllyPlayerName", "NA9", "Jinx", 100, win),
             player(6, "enemy-puuid-1111-bbbb", "EnemyPlayerName", "EUW7", "Syndra", 200, not win)]
    return {"metadata": {"matchId": match_id, "participants": [p["puuid"] for p in parts]},
            "info": {"gameStartTimestamp": start, "gameDuration": 1500, "queueId": 420,
                     "gameVersion": "16.18.1", "participants": parts}}


def timeline():
    frames = []
    for minute in range(4):
        frames.append({"timestamp": minute * 60_000, "events": [], "participantFrames": {
            str(pid): {"totalGold": 500 + 300 * minute + pid, "xp": 200 * minute,
                       "minionsKilled": 8 * minute, "jungleMinionsKilled": 0,
                       "position": {"x": 7000, "y": 7000}} for pid in (1, 2, 6)}})
    frames[2]["events"].append({"type": "CHAMPION_KILL", "timestamp": 130_000, "victimId": 1,
                                "killerId": 6, "position": {"x": 7000, "y": 7200}})
    return {"info": {"frameInterval": 60_000, "frames": frames}}


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.db, self.notes, self.out = base / "league.db", base / "reviews.db", base / "out"
        with closing(sqlite3.connect(self.db)) as c:
            c.executescript(SCHEMA)
            for i in range(1, 4):
                mid = f"NA1_700000000{i}"
                store_match(c, riot_match(mid, 1_000_000 * i, i % 2 == 1), ME)
                store_timeline(c, mid, timeline())
        store = ReviewStore(self.db, self.notes)
        store.save_focus({"goal": "Private goal about EnemyPlayerName"})
        store.save_review({"match_id": "NA1_7000000003", "moment_id": "custom-60000",
                           "start_ms": 60000, "end_ms": 60000, "reason": "other",
                           "observation": "my private note", "alternative": "",
                           "evidence": "memory"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_identifier_survives(self):
        files = build(self.db, self.notes, count=2, riot_id="MySecretName#NA9")
        write(files, self.out)
        text = "".join(p.read_text(encoding="utf-8") for p in self.out.rglob("*.json"))
        for secret in SECRETS:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, text)
        for word in ("puuid", "riotId", "summoner", "my private note", "Private goal"):
            self.assertNotIn(word, text)

    def test_files_and_shapes(self):
        files = build(self.db, self.notes, count=2)
        self.assertEqual(set(files), {"matches.json", "match/demo-1.json", "match/demo-2.json",
                                      "focus.json", "charm.json", "profile.json", "recorder.json"})
        self.assertEqual([m["match_id"] for m in files["matches.json"]], ["demo-1", "demo-2"])
        self.assertEqual(files["matches.json"][0]["opp_champion"], "Syndra")
        detail = files["match/demo-1.json"]
        self.assertEqual(set(detail), {"match", "frames", "deaths", "moments", "reviews",
                                       "cadence_ms", "recording"})
        self.assertEqual((detail["match"]["match_id"], detail["reviews"], detail["recording"]),
                         ("demo-1", [], None))
        self.assertEqual(detail["deaths"], [130_000])
        charm = files["charm.json"]
        self.assertEqual([g["match_id"] for g in charm["games"]], ["demo-1", "demo-2"])
        self.assertEqual(charm["summary"]["all"]["n"], 2)
        self.assertEqual([c["champion"] for c in files["profile.json"]["champions"]], ["Ahri"])
        self.assertEqual(files["focus.json"], {"goal": "", "why_text": "", "check_text": ""})
        self.assertEqual(files["recorder.json"]["armed"], False)

    def test_include_notes_is_opt_in_and_still_checked(self):
        with self.assertRaises(ScrubError):  # the focus text names another player
            build(self.db, self.notes, include_notes=True)
        ReviewStore(self.db, self.notes).save_focus({"goal": "0 deaths before 10:00"})
        files = build(self.db, self.notes, include_notes=True)
        self.assertEqual(files["focus.json"]["goal"], "0 deaths before 10:00")
        self.assertEqual(files["match/demo-1.json"]["reviews"][0]["match_id"], "demo-1")

    def test_check_scrubbed_ignores_champion_names(self):
        files = {"matches.json": [{"my_champion": "Ahri", "opp_champion": "Zed"}]}
        check_scrubbed(files, {"Zed", "ab"})  # a player named like a champion; too-short id
        with self.assertRaises(ScrubError):
            check_scrubbed(files | {"x": "hello EnemyPlayerName"}, {"EnemyPlayerName"})

    def test_source_databases_unchanged(self):
        before = self.db.read_bytes()
        build(self.db, self.notes)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(json.loads(json.dumps(build(self.db, self.notes)))["recorder.json"]["last"],
                         None)


if __name__ == "__main__":
    unittest.main()
