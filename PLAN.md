# LoLCoach Plan

A personal, local-first, TOS-safe coach for Ahri mid. It builds on the existing fetcher,
CLI and review app. Last updated 2026-09-22.

## Why
- Returning to ranked on Ahri mid; struggling with decisions and Charm (E) accuracy.
- Winning on Lulu support (5-2), so the gap is Ahri-specific rather than general game sense.
- Goal: turn post-game review into **one practice focus at a time**, measured over the
  next games. The app is not a stats dashboard.

## Constraints
### Known
- **No ban risk / TOS-safe.** Official Riot APIs, your own recordings and your own replays
  only. No memory reading, input hooks, overlays, or anything shown during a game.
  Champ select and post-game only.
- **Local-first.** Match data, notes, video and audio stay on this machine. Only the
  Claude head coach calls the cloud, and it gets your own match data with other
  players' identifiers stripped.
- **Privacy.** `*.db`, `data/`, `.vs/` and `.idea/` are git-ignored because the databases
  hold other players' PUUIDs. `.env` holds the API keys.
- **Honest signals.** Every signal is a review prompt, not a diagnosis. Every claim
  cites its evidence and shows its uncertainty. This keeps the tone of `review_app.py`.
- **Dependencies.** `review_app.py` stays standard-library only. ML code goes in a
  separate `coach/` package with its own `requirements-ml.txt`.
- **Hardware.** Core Ultra 7 265K, 96 GB DDR5, RTX 5080 16 GB, RTX 5060 Ti 16 GB.
  Blackwell needs torch with CUDA 12.8+.
- **Riot API limits.** Development keys expire every 24 hours, and the limits are
  20 requests/s and 100 requests/2 min. Long crawls need a Personal API Key.

### UI
- **No "vibe-coded" look.** See the banned patterns in `AGENTS.md`.
- **Design direction:** an early-2000s gaming fansite with League/Hextech-inspired
  materials, specified in `UI.md`. Inspired by League, not copied: no Riot logos,
  fonts or client UI.

### To add
_TBD: more constraints from the user go here._

## Where we are
- [x] `fetch_matches.py`: Riot API → `league.db` (matches, frames, events, raw JSON)
- [x] `analyze.py`: matchups, backs, roams, death map
- [x] `review_app.py`: local review UI, moments, notes, focus, manual video sync
- [x] Step 0: DBs and IDE folders git-ignored and unstaged
- [x] `analyze.py charm`: Charm estimate = `enemyChampionImmobilizations / spell3Casts`
- [x] `analyze.py profile`: end-of-game habits per champion

**First findings** (21 Ahri, 7 Lulu games; small sample):

| | Charm est. | deaths/10m | dmg taken share | vision/min |
|---|---|---|---|---|
| Ahri | 50% (56% W / 47% L) | 2.2 | 18% | 1.25 |
| Lulu | – | 2.0 | 15% | 2.75 |

On Ahri you take a large share of the team's damage for a squishy mid, which points
to positioning, not only aim. That is a lead to check against video, not a conclusion.

## Positioning vs existing tools
- **Don't compete with:** stat dashboards (op.gg, Mobalytics, iTero's 500+ stats) or
  generic win-prob grades.
- **Our edge:** personal comparison (you on Lulu vs you on Ahri), a metric for one
  champion's mechanic, local video synced to the Riot timeline, evidence-labelled
  reviews, and a closed practice loop.

## Roadmap

### Phase 1: Charm stats, profile and the practice loop
- [ ] Confidence intervals on the Charm and profile numbers, bootstrapped per game
- [ ] Charm panel in the review app (per-game trend, W/L, opponent)
- [ ] Make **Today's focus** the centre: one goal, a measurable check (e.g. "Charm est.
      ≥ 55%" or "0 deaths before 10:00"), then graded automatically over the next 3–5 games
- [ ] Matchup card for champ select: your notes plus Charm and death stats vs that opponent
- [ ] Apply for a Riot Personal API Key

### Phase 2: Video v1 (auto-sync and death clips)
- [ ] **`recorder.py` OBS auto-record (decided: a button arms it for the session, off by default)**
  - "Auto-record: On/Off" toggle in the review app → `POST /api/recorder`
  - When armed and OBS isn't running, launch
    `C:\Program Files\obs-studio\bin\64bit\obs64.exe --minimize-to-tray --disable-shutdown-check`
    (OBS 32.0.4 installed; obs-websocket v5 on :4455, password from `.env` `OBS_WS_PASSWORD`)
  - Game detection: poll `https://127.0.0.1:2999/liveclientdata/gamestats` every 2 s
    (official, read-only, only answers in game; self-signed cert → no verification, localhost only)
  - Game starts → `StartRecord`, read `gameTime` → store the offset. Game ends (API
    stops answering) → `StopRecord` and take the output path from the response.
  - Link the recording to its `match_id` by game start time after the next fetch.
    Save it in a `recordings` table in `reviews.db`: match_id, path, offset_s,
    started_at, status.
  - Review app auto-loads the linked recording and offset. The manual offset stays as a fallback.
  - Never shows anything in game. No LCU, no input hooks.
- [ ] Fallback: Replay API clips of flagged moments (needs `EnableReplayApi=1`; replays are patch-locked)
- [ ] **Clock OCR** (PaddleOCR) → offset only for recordings made outside `recorder.py`
- [ ] Automatic clip extraction: 60 s before each death and each flagged moment
- [ ] Review app plays those clips inline

### Phase 3: Video v2, the Charm detector
- [ ] HUD template match: E icon goes on cooldown → exact cast time
- [ ] Fine-tuned YOLO / RT-DETR: "charmed" status on enemies → hit or miss
- [ ] Label frames with SAM 2 assistance; hold out a labelled test set
- [ ] Split misses into **aim** (target walked straight) vs **decision** (target
      could easily walk around it)
- [ ] Validate the Phase 1 API estimate against video-counted hits
- [ ] Minimap positions: bootstrap from the DeepLeague and synthetic Hugging Face datasets

### Phase 4: Win-probability model
- [ ] `crawl.py` → `corpus.db`: Master+ ranked games from `league-v4` seeds,
      20–50k matches, Ahri games tagged
- [ ] LightGBM baseline on per-minute state, then an event-sequence transformer on the 5080
- [ ] Calibration (Brier score, reliability curve), holding out by patch/date
- [ ] Use it to **choose which moments to review** (`kind='wp_swing'`), not to grade you
- [ ] Per-minute Ahri baseline from high-elo games

### Phase 5: Voice debrief
- [ ] Record button in the review app (`MediaRecorder`) → faster-whisper large-v3 on the
      5060 Ti → `debriefs` table
- [ ] Session/tilt signals: transcript, games in session, loss streaks, time of day

### Phase 6: Claude head coach
- [ ] `coach/coach.py`: Anthropic SDK agent with tools over the local DBs (match summary,
      moments, Charm stats, win-prob curve, debrief, clip captions, baseline)
- [ ] Output: cited, hypothesis-labelled review plus a proposed focus. You accept it into
      the `focus` table.
- [ ] `claude-opus-5-5` for reviews, `claude-sonnet-5` for summaries, with prompt caching

### Demo on AWS (decided: static site with sample data)
- [ ] `export_demo.py`: builds `demo/` with the review UI plus a JSON snapshot of about 5
      matches. Other players' PUUIDs and names are removed and replaced with champion
      names only, and the user's Riot ID is dropped. No notes unless opted in.
- [ ] UI data layer is swappable: `/api/*` locally, `demo/data/*.json` in demo mode
      (read-only; save buttons disabled and labelled "demo").
- [ ] Hosting: private S3 bucket + CloudFront (OAC), HTTPS, about $1/month. Deploy
      with `aws s3 sync` + invalidation in `deploy_demo.ps1`. No server, no API keys.
- [ ] Riot notice in the footer. A read-only personal demo, not a public product.

## Workstreams (two agents in parallel)
Both follow `AGENTS.md`. Each works in its own git worktree/branch and merges via PR.
File ownership avoids conflicts; changes to the shared JSON API contract go in
`API.md` first.

| | **Core agent** | **UI agent** |
|---|---|---|
| Owns | all `*.py` (incl. `export_demo.py`), `coach/`, `test_*.py`, DB schemas, `API.md` | `review_web/**`, `demo/` front end, fonts/textures, `deploy_demo.ps1` |
| First tasks | 1. `API.md`: document current `/api/*` JSON. 2. Confidence intervals + `/api/charm`, `/api/profile`. 3. `recorder.py` + `/api/recorder` + `recordings` table, with tests (mock OBS and Live Client). 4. `export_demo.py` | 1. Restyle `review_web` to `UI.md` (remove all AGENTS.md tells). 2. Nav tabs: Review / Charm / Profile. 3. Charm and Profile views from `API.md` (mock JSON until core lands). 4. Auto-record toggle + status line. 5. Demo mode data layer |
| Done when | `python -m unittest -v` green, endpoints match `API.md` | `UI.md` §10 checklist passes, works on real `/api/*` and demo JSON |

## Video architecture
General VLMs are poor at tiny, fast game details, so specialised models extract the
facts and the language models explain them.

| Layer | Job | Models / tools | GPU |
|---|---|---|---|
| 1. Facts | clock, E casts, charm hits, minimap | PaddleOCR, template matching, YOLO/RT-DETR, SAM 2 (labelling) | 5080 train, 5060 Ti infer |
| 1b. Search | "all my river deaths" | SigLIP 2 / CLIP embeddings | 5060 Ti |
| 2. Clip captions | describe a flagged clip | Qwen3-VL (small dense on one card; 30B-A3B 4-bit split across both), InternVL 3.5, GLM-4.6V to benchmark | 5080 (+5060 Ti) |
| 3. Coach | reason over the event log plus key frames | Claude API | cloud |

## Verification
- `python -m unittest -v` stays green, with tests alongside each new module
- Charm: the API estimate agrees with video-counted hits on labelled games
- Detectors: precision/recall on a held-out labelled set
- Win-prob: held-out Brier/log-loss and a calibration plot; top swings in 3 of your
  games match your memory or VOD
- Coach: spot-check 5 claims per report against the DB
- Practice loop: does the focus metric move over the following games?
