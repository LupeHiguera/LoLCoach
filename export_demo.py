#!/usr/bin/env python3
"""
Export a small, scrubbed JSON snapshot for the static demo (see API.md, "Demo snapshot").

    python export_demo.py                    # newest 5 Ahri/Zoe mid games → demo/data/
    python export_demo.py --count 3 --include-notes

What is removed: match ids (replaced with demo-1..N, since a real id reveals every
player), every PUUID, Riot ID, summoner name and id, and the linked recording. Other
players appear only as champion names. Notes and focus are left out unless
--include-notes. Before writing, the output is checked for every identifier in the
source matches and the export is refused if one survives.

Never commit a demo/data/ built from real data without reading it first.
"""
import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import analyze
from fetch_matches import SCHEMA, load_dotenv
from review_app import ReviewStore, connect

ROOT = Path(__file__).resolve().parent
DEFAULT_COUNT = 5
# Participant fields that identify a player. Anything else in the raw JSON is game stats.
IDENTITY_KEYS = ("puuid", "riotIdGameName", "riotIdName", "riotIdTagline", "summonerName",
                 "summonerId", "accountId")
# Identifier strings shorter than this are not checked (too many false positives).
MIN_CHECK_LEN = 3
RECORDER_DEMO = {"armed": False, "obs": "unavailable", "game": "idle", "last": None}


class ScrubError(RuntimeError):
    """An identifier from the source data would have been written to the demo."""


def raw_match(conn, match_id):
    return json.loads(conn.execute("SELECT raw_json FROM matches WHERE match_id = ?",
                                   (match_id,)).fetchone()[0])


def identifiers(conn, match_ids, riot_id=None):
    """Every identifying string in the source matches: ids, PUUIDs, names, tags."""
    found = set(match_ids)
    for match_id in match_ids:
        raw = raw_match(conn, match_id)
        found.update(raw.get("metadata", {}).get("participants", []))
        for p in raw.get("info", {}).get("participants", []):
            # A bare tagline ("NA1", "123") would match unrelated numbers; check name#tag.
            found.update(str(p[k]) for k in IDENTITY_KEYS
                         if k != "riotIdTagline" and p.get(k) not in (None, ""))
            if p.get("riotIdGameName") and p.get("riotIdTagline"):
                found.add(f"{p['riotIdGameName']}#{p['riotIdTagline']}")
        found.update(r[0] for r in conn.execute(
            "SELECT puuid FROM participants WHERE match_id = ?", (match_id,)) if r[0])
    if riot_id:
        found.update({riot_id, riot_id.split("#")[0]})
    return found


def stats_only(raw, my_participant_id):
    """Just my end-of-game stats, with identity fields dropped (input to charm/profile)."""
    me = next(p for p in raw["info"]["participants"] if p.get("participantId") == my_participant_id)
    me = {k: v for k, v in me.items() if k not in IDENTITY_KEYS}
    return {"info": {"participants": [me]}}


def scrubbed_stats_db(conn, id_map):
    """In-memory league DB holding only the exported matches, renamed and stripped."""
    mem = sqlite3.connect(":memory:")
    mem.executescript(SCHEMA)
    for real, demo in id_map.items():
        row = conn.execute("SELECT game_start_ms, duration_s, patch, queue_id, my_participant_id, "
                           "my_champion, my_position, win, opp_participant_id, opp_champion "
                           "FROM matches WHERE match_id = ?", (real,)).fetchone()
        raw = stats_only(raw_match(conn, real), row[4])
        mem.execute("INSERT INTO matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (demo, *row, json.dumps(raw)))
    return mem


def build(league_path, notes_path, count=DEFAULT_COUNT, include_notes=False, riot_id=None):
    """{relative path: JSON-able data} for demo/data. Raises ScrubError on a leak."""
    store = ReviewStore(league_path, notes_path)
    chosen = store.matches()[:count]
    id_map = {m["match_id"]: f"demo-{i}" for i, m in enumerate(chosen, 1)}
    files = {"matches.json": [dict(m, match_id=id_map[m["match_id"]]) for m in chosen]}
    for real, demo in id_map.items():
        detail = store.detail(real)
        detail["match"]["match_id"] = demo
        detail["recording"] = None
        detail["reviews"] = ([dict(r, match_id=demo) for r in detail["reviews"]]
                             if include_notes else [])
        files[f"match/{demo}.json"] = detail
    files["focus.json"] = (store.focus() if include_notes
                           else {"goal": "", "why_text": "", "check_text": ""})
    with closing(connect(league_path, True)) as conn:
        with closing(scrubbed_stats_db(conn, id_map)) as mem:
            files["charm.json"] = analyze.charm_report(mem)
            files["profile.json"] = analyze.profile_report(mem)
        forbidden = identifiers(conn, list(id_map), riot_id)
    files["recorder.json"] = dict(RECORDER_DEMO)
    check_scrubbed(files, forbidden)
    return files


def check_scrubbed(files, forbidden):
    """Refuse the export if any identifier string appears anywhere in the output."""
    champions = {m[k] for m in files.get("matches.json", [])
                 for k in ("my_champion", "opp_champion") if m.get(k)}
    champions.update(name for rel, data in files.items() if rel.startswith("match/")
                     for k in data.get("kills", [])
                     for name in [k.get("killer"), k.get("victim"), *k.get("assists", [])] if name)
    text = json.dumps(files, ensure_ascii=False)
    leaks = sorted(s for s in forbidden
                   if len(s) >= MIN_CHECK_LEN and s not in champions and s in text)
    if leaks:
        raise ScrubError(f"{len(leaks)} identifier(s) would be exported; nothing was written.")


def write(files, out_dir):
    out = Path(out_dir)
    for rel, data in files.items():
        path = out / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=ROOT / "league.db")
    ap.add_argument("--notes", type=Path, default=ROOT / "reviews.db")
    ap.add_argument("--out", type=Path, default=ROOT / "demo" / "data")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--include-notes", action="store_true",
                    help="Also export your review notes and focus (read them first)")
    args = ap.parse_args()
    try:
        files = build(args.db, args.notes, args.count, args.include_notes,
                      os.environ.get("RIOT_ID"))
    except ScrubError as exc:
        sys.exit(f"Export refused: {exc}")
    except (sqlite3.Error, OSError, ValueError) as exc:
        sys.exit(f"Cannot export: {exc}")
    write(files, args.out)
    print(f"Wrote {len(files)} files for {len(files['matches.json'])} matches to {args.out}")


if __name__ == "__main__":
    main()
