# LoLCoach local API

The JSON contract between `review_app.py` (Core agent) and `review_web/` plus the static
demo (UI agent). Change this file first, then the code.

- Server: `python review_app.py` → `http://127.0.0.1:8765`. It binds to 127.0.0.1 only.
- `league.db` is opened read-only. Notes, focus and recordings live in `reviews.db`.
- All responses are JSON (`Content-Type: application/json; charset=utf-8`) unless noted,
  with `Cache-Control: no-store`.
- Times: `*_ms` are milliseconds (epoch ms for `game_start_ms` and `start`, game-clock
  ms elsewhere). `*_s` are seconds. Rates and shares are 0–1 floats, never percentages.
- `null` means "not known", never zero. Render it as "–", not "0".

Status legend: **current** = in `review_app.py` on `foundation`; **new** = added on
`core/phase1`.

## Errors

Every error body is `{"error": "<message for the user>"}`. The message is safe to show.

| Status | When |
|---|---|
| 400 | POST body fails validation (message says which field) |
| 403 | `Host` is not `127.0.0.1:<port>` or `localhost:<port>` → `"Local access only"` |
| 403 | POST with an `Origin` header that is not `http://<Host>` → `"Origin rejected"` |
| 404 | Unknown path → `"Not found"`; unknown match id → `"Match not found"` |
| 415 | POST without `Content-Type: application/json` → `"JSON required"` |
| 503 | SQLite error (e.g. database locked during a fetch). GET: `"Unable to read the database. Retry after the import finishes."`; POST: `"Could not save. Your text is still here; please retry."` |

POST rules: body must be a JSON object of 1–20000 bytes, otherwise 400
(`"Invalid request size"` / `"Expected an object"`).

## Static files (current, extended)

`GET /` serves `review_web/index.html`. Any other `GET /<path>` serves
`review_web/<path>` if it exists, stays inside `review_web/`, and has one of these
extensions: `.html .js .css .woff2 .woff .ttf .png .jpg .jpeg .webp .svg .ico .json`.
Anything else is 404. (On `foundation` only `/`, `/app.js`, `/style.css` were served;
subfolders such as `/fonts/x.woff2` now work.)

CSP: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'`.

---

## GET /api/matches (current)

Ahri or Zoe games in `MIDDLE` that have a timeline, newest first. No parameters.

```json
[
  {"match_id": "NA1_5646221173", "game_start_ms": 1789967389349, "duration_s": 1957,
   "patch": "16.18", "queue_id": 420, "my_champion": "Ahri", "my_position": "MIDDLE",
   "opp_champion": "Akali", "win": 0}
]
```

`win` is `0`/`1`. `opp_champion` can be `null` (no lane opponent found). Empty list when
there are no games.

## GET /api/match?id=<match_id> (current, `recording` is new)

One game for review. 404 `"Match not found"` for an unknown or missing `id`.

```json
{
  "match": {"match_id": "NA1_5646221173", "game_start_ms": 1789967389349,
            "duration_s": 1957, "patch": "16.18", "queue_id": 420,
            "my_champion": "Ahri", "my_position": "MIDDLE", "opp_champion": "Akali",
            "win": 0, "my_participant_id": 3, "opp_participant_id": 8},
  "frames": [
    {"time": 60010, "gold": 500, "xp": 0, "cs": 0, "jungle_cs": 0,
     "gold_diff": 0, "xp_diff": 0, "cs_diff": 0}
  ],
  "deaths": [303341, 483672],
  "moments": [
    {"id": "death-303341", "start_ms": 243341, "end_ms": 303341, "title": "First death",
     "kind": "death", "description": "Review the minute before this death. ..."}
  ],
  "reviews": [
    {"match_id": "NA1_5646221173", "moment_id": "death-303341", "start_ms": 243341,
     "end_ms": 303341, "reason": "fighting", "observation": "...", "alternative": "...",
     "evidence": "recording", "updated_at": "2026-09-22T21:00:00+00:00"}
  ],
  "cadence_ms": 60000,
  "recording": {"path": "C:\\Users\\me\\Videos\\2026-09-22 20-01-03.mp4",
                "offset_s": -12.4, "status": "linked",
                "url": "/api/recording-file?id=NA1_5646221173"}
}
```

- `frames[].time` is game-clock ms of each timeline snapshot (about every `cadence_ms`).
- `*_diff` = me minus the lane opponent at the same snapshot; `null` when the opponent
  has no snapshot at exactly that time.
- `deaths` are game-clock ms of my deaths.
- `moments[].kind` is `death` | `deficit` | `farm`. They are review prompts, not mistakes.
- `reviews` are the user's saved notes for this match (ordered by `start_ms`).
- `recording` (**new**) is `null` when no recording is linked to this match. Otherwise:
  - `path`: local file path (display only; the browser cannot open it directly).
  - `offset_s`: seconds to **add** to game time to get video time
    (`video_s = game_ms / 1000 + offset_s`), the same meaning as the manual offset
    field. Negative when recording started after the game clock was already running.
    Can be `null` if the game clock was not read; fall back to the manual offset.
  - `status`: see `/api/recorder` statuses.
  - `url`: same-origin URL that streams the video (supports HTTP `Range`), or `null` when
    the file no longer exists on disk.

## GET /api/recording-file?id=<match_id> (new)

Streams the linked recording for a match as `video/mp4`, `video/x-matroska`,
`video/webm` or `application/octet-stream` (by extension), with `Accept-Ranges: bytes`
and `206 Partial Content` for `Range` requests. 404 `"No recording for this match"`
when nothing is linked or the file is gone. Only files registered in the `recordings`
table are ever served.

## GET /api/focus (current)

```json
{"goal": "0 deaths before 10:00", "why_text": "3 of my last 5 games...",
 "check_text": "Deaths before 10:00 in the next 5 games"}
```

All three are `""` when no focus has been saved.

## POST /api/focus (current)

Body: `{"goal": str ≤1000, "why_text": str ≤1000, "check_text": str ≤1000}`. Strings
are trimmed. Missing keys count as `""`. Response: the saved focus (same shape as GET).
400 `"Write a practice goal first."` if `goal` is blank; 400
`"Invalid or too long: <key>"` if a value is not a string or too long.

## POST /api/reviews (current)

Create or replace the note for one moment (key: `match_id` + `moment_id`).

```json
{"match_id": "NA1_5646221173", "moment_id": "death-303341",
 "start_ms": 243341, "end_ms": 303341, "reason": "fighting",
 "observation": "Took the trade with no wave.", "alternative": "Wait for the wave.",
 "evidence": "recording"}
```

| Field | Rule |
|---|---|
| `match_id` | must exist (else 404-style `KeyError` → **400** `"'Match not found'"`) |
| `moment_id` | non-empty string ≤100 (custom moments use e.g. `custom-120000`) |
| `start_ms`, `end_ms` | integers (not booleans), `0 ≤ start ≤ end ≤ match length` |
| `reason` | `uncertain` `dead` `recalling` `fighting` `roaming` `pressured` `other` |
| `observation` | non-empty string ≤3000 |
| `alternative` | string ≤3000 |
| `evidence` | `stats_only` `recording` `memory` |

Response: `{"saved": true}`.

---

## GET /api/charm (new)

Ahri Charm (E) estimate per game and pooled, with bootstrap confidence intervals.

**What it is:** `hits / casts`, where `hits` is Riot's end-of-game challenge stat
`enemyChampionImmobilizations` and `casts` is `spell3Casts`. Charm is Ahri's only
crowd control, so this approximates a Charm hit rate. **What it isn't:** a per-cast log;
it can't tell which casts hit, and one Charm can immobilize more than one enemy.

```json
{
  "games": [
    {"match_id": "NA1_5646221173", "start": 1789967389349, "opponent": "Akali",
     "win": false, "casts": 31, "hits": 14, "rate": 0.4516, "per_min": 0.95}
  ],
  "summary": {
    "all":    {"n": 21, "rate": 0.50, "ci": [0.44, 0.56]},
    "wins":   {"n": 9,  "rate": 0.56, "ci": [0.47, 0.64]},
    "losses": {"n": 12, "rate": 0.47, "ci": [0.40, 0.54]}
  },
  "by_opponent": [
    {"opponent": "Syndra", "n": 3, "rate": 0.41, "ci": [0.33, 0.52]},
    {"opponent": "Akali", "n": 1, "rate": 0.45, "ci": null}
  ],
  "method": {"ci_level": 0.9, "iterations": 2000, "resample": "game"}
}
```

- `games`: every Ahri game with both stats, **newest first**. `start` is epoch ms.
  `win` is a boolean. `opponent` can be `null`. `per_min` = casts per game minute,
  `null` if the duration is unknown.
- `summary.*.rate` is pooled: total hits / total casts, so a long game with many casts
  weighs more. `rate` is `null` and `ci` is `null` when `n` is 0.
- `ci` is `[lo, hi]` from a percentile bootstrap that resamples **games** (not casts)
  with replacement, at `method.ci_level`. `null` when `n < 2`. Seeded, so the same data
  always gives the same interval. With few games the interval is wide; show it.
- `by_opponent` is sorted by `n` descending, then name. Opponent `null` is grouped as
  `null`.
- `method` (additive to the draft) describes how `ci` was computed, for a caption.

## GET /api/profile (new)

End-of-game habits per champion the user has played (any role, any queue), with the
same per-game bootstrap.

```json
{
  "champions": [
    {"champion": "Ahri", "n": 21, "wins": 9,
     "stats": [
       {"key": "deaths_per_10m", "label": "deaths/10m", "value": 2.2, "ci": [1.9, 2.6], "unit": "per_10m", "n": 21},
       {"key": "kill_participation", "label": "kp", "value": 0.52, "ci": [0.48, 0.56], "unit": "ratio", "n": 21},
       {"key": "damage_share", "label": "dmg share", "value": 0.27, "ci": [0.25, 0.29], "unit": "ratio", "n": 21},
       {"key": "damage_taken_share", "label": "taken share", "value": 0.18, "ci": [0.17, 0.20], "unit": "ratio", "n": 21},
       {"key": "vision_per_min", "label": "vision/m", "value": 1.25, "ci": [1.1, 1.4], "unit": "per_min", "n": 21},
       {"key": "time_dead_share", "label": "dead %", "value": 0.08, "ci": [0.06, 0.10], "unit": "ratio", "n": 21}
     ]}
  ],
  "method": {"ci_level": 0.9, "iterations": 2000, "resample": "game"}
}
```

- `champions` sorted by `n` descending. All champions are returned, including 1-game
  ones; `ci` is `null` when a stat's `n < 2`. The CLI hides champions under 3 games; the
  UI should at least de-emphasise small `n`.
- `stats` order and keys come from `analyze.PROFILE_STATS` and are stable.
- `value` is the mean over games that have the stat (`null` if none). `n` per stat can
  be lower than the champion `n` when older match JSON lacks a challenge field.
- `unit`: `ratio` (0–1, show as %), `per_10m` (per 10 game minutes), `per_min`.
- Different roles have different norms (Lulu support vs Ahri mid): compare habits,
  not raw numbers.

## GET /api/recorder (new)

OBS auto-record state. Reading it never starts anything.

```json
{"armed": false, "obs": "running", "game": "idle",
 "last": {"match_id": "NA1_5646221173",
          "path": "C:\\Users\\me\\Videos\\2026-09-22 20-01-03.mp4",
          "status": "linked", "started_at": "2026-09-22T20:01:03+00:00"}}
```

- `armed`: auto-record toggle. Always `false` when the app starts.
- `obs`: `running` (obs-websocket answered), `stopped` (OBS process/websocket not
  reachable), `unavailable` (OBS not installed at the expected path, or
  `OBS_WS_PASSWORD` missing from `.env`, or authentication failed).
- `game`: `in_game` while the Live Client Data API answers on `127.0.0.1:2999`,
  otherwise `idle`. Only polled while armed; `idle` when disarmed.
- `last`: the newest row of the `recordings` table, or `null`.
  - `match_id` is `null` until a fetch stores the match and the recording is linked.
  - `status`: `recording` (in progress) | `saved` (stopped, waiting for the match to be
    fetched) | `linked` (matched to `match_id`) | `failed` (OBS error; `path` may be
    `null`).
  - `started_at`: ISO 8601 UTC when `StartRecord` succeeded.
- `error` (optional, additive): short message for the last OBS/recorder problem, e.g.
  `"OBS_WS_PASSWORD is not set in .env"`. Absent when there is none.

## POST /api/recorder (new)

Body `{"armed": true|false}`. Response: same shape as GET. 400 `"armed must be true or
false"` for anything else. Goes through the same Host/Origin checks as other POSTs.
Arming starts the background poller and launches OBS minimised to tray if it isn't
running. Disarming stops polling; a recording already in progress is stopped when the
game ends, not cut off.

Nothing is ever shown in game. No LCU, no input hooks.

---

## Demo snapshot (`demo/data/*.json`, new)

`python export_demo.py` writes a read-only snapshot with the same shapes as the API:

| File | Same shape as |
|---|---|
| `demo/data/matches.json` | `GET /api/matches` |
| `demo/data/match/<match_id>.json` | `GET /api/match?id=<match_id>` |
| `demo/data/focus.json` | `GET /api/focus` |
| `demo/data/charm.json` | `GET /api/charm` |
| `demo/data/profile.json` | `GET /api/profile` |
| `demo/data/recorder.json` | `GET /api/recorder` (always disarmed, `last: null`) |

Differences in the demo:
- Match ids are replaced with `demo-1` … `demo-N` (a real match id can be looked up to
  reveal every player), so `match/<match_id>.json` is e.g. `match/demo-1.json`.
- `recording` is always `null`; `reviews` is `[]` and `focus` is empty unless the export
  was run with `--include-notes`.
- Other players appear only as champion names; no PUUIDs, Riot IDs or summoner names.
- `charm.json` and `profile.json` are computed from the exported matches only.
