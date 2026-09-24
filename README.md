# League review (personal project)

Pulls my recent Ahri/Zoe games and their timelines from the Riot API into a local SQLite database for post-game review.

## Setup

1. `pip install requests`
2. Get a development key at https://developer.riotgames.com (expires every 24 hours).
3. Copy `.env.example` to `.env` and fill in your key and Riot ID.
4. `python fetch_matches.py --count 20` (add `--queue 420` for ranked solo/duo only)

Re-running only downloads new matches. `--summary-only` prints the lane table without calling the API.

## Local review app

Run `python review_app.py` (Python 3.10+, standard library only), then open
http://127.0.0.1:8765 (on Windows, `py review_app.py` works if `python` opens the Microsoft
Store). Keep the terminal running; Ctrl+C stops the app.

The app opens ranked solo/duo by default and lets you filter Ahri/Zoe mid games by
champion, queue, and result. Review gold/XP/lane-CS differences over the full match,
choose a death, sampled gold deficit, farm gap, or any timeline snapshot, and save
your observation and a possible next action. Set one practice goal in Today's focus.

Optional video selection uses a local browser object URL: footage is not uploaded.
Set the recording time (seconds) corresponding to game 0:00 and use Jump to review
start. Choose the recording again after a reload or match switch. Cuts or pauses
require manually adjusting the offset. Browser codec support varies (MP4/WebM recommended).

`league.db` is opened read-only. Notes and the current focus persist in the separate,
git-ignored `reviews.db`; back up both files to keep your imports and reviews.
Use `--db PATH`, `--notes PATH`, or `--port 8766` to override defaults.

Review prompts are deliberately limited:

- Death timestamps are events; the preceding minute is context to inspect.
- The first sampled gold difference of -300 or below is a configurable-in-code
  review threshold, not evidence of a mistake or the cause of a loss.
- Farm gaps mean no increase in lane CS for at least two minutes after 2:00.
  Missing snapshots break a gap; jungle CS is excluded. They do not prove missed
  last hits, roaming, or lost opportunity.
- Gold/XP/CS differences use matching timestamps for the assigned lane opponent;
  role swaps and later team play need interpretation. Missing comparisons stay missing.
- Causes are user-entered hypotheses; saved reviews identify whether evidence was
  timeline data, a recording, or recollection. No AI or live-game integration is used.

Verify with `python -m unittest -v`.

## Command-line analysis

`analyze.py` reads the database and never calls the API, so it works on an expired key.

```
python analyze.py                     # all four sections
python analyze.py matchups            # averages at 10:00, per opponent
python analyze.py backs               # first recall timing, and if a death forced it
python analyze.py roams               # minutes spent off mid before 15:00
python analyze.py deaths --matchup Syndra   # ASCII map of early deaths
python analyze.py charm               # Ahri Charm estimate per game, W/L, trend, opponent
python analyze.py profile             # end-of-game habits per champion (Ahri vs Lulu)
```

`charm` and `profile` read end-of-game stats from `matches.raw_json`, so they include
games without a timeline (Lulu games are stored but their timelines aren't fetched).

- **charm** - `challenges.enemyChampionImmobilizations / spell3Casts`, pooled across
  games. Charm is Ahri's only crowd control, so this approximates a Charm hit rate; it is
  an end-of-game estimate, not a per-cast log, and win/loss gaps do not show cause.

`--minute N` moves the lane snapshot (default 10), `--champions` and `--db` match
`fetch_matches.py`, `--matchup NAME` filters to one opponent, and `--min-games N`
hides matchups you have played fewer than N times. The matchup table always ends
with a per-champion rollup, since most individual matchups are one or two games.

How the derived numbers are defined:

- **first back** - a purchase-based estimate: first purchase group after 2:00,
  with purchases within 12s grouped. The 25s death lookback can miss death-related
  shop visits; this does not establish recall time or whether a recall was voluntary.
- **roam** - consecutive snapshots more than ~2200 units off the mid diagonal and
  outside both bases. Even a short trip can coincide with a snapshot; counted
  snapshots do not establish actual duration, complete roam counts, or a lower bound.
- **cs in lane** vs **cs on roam** - heuristic CS rates assigned using sampled
  positions. These do not measure the opportunity cost of roaming.

## Database

- `matches`: one row per match you played (any champion), with your champion, role, result and lane opponent
- `participants`: end-of-game stats for all ten players
- `timelines` / `frames`: per-minute gold, XP, level, CS and position for every player
- `events`: timestamped kills, item purchases, skill level-ups, wards, plates and objectives
- Raw API JSON is kept in `matches.raw_json` and `timelines.raw_json`

The database contains other players' identifiers, so it is git-ignored and should stay local.

---

This project isn't endorsed by Riot Games and doesn't reflect the views or opinions of Riot Games or anyone officially involved in producing or managing Riot Games properties. Riot Games, and all associated properties are trademarks or registered trademarks of Riot Games, Inc.
