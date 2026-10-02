# LoLCoach Plan

A personal, local-first, TOS-safe coach for Ahri mid. It builds on the existing fetcher,
CLI and review app. Last updated 2026-10-02.

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
- **Local-first.** Databases, notes, video, frames and audio stay on this machine.
  OpenAI or Claude may provide post-game coaching from sanitised text evidence
  only. Strip player names, PUUIDs, Riot IDs, local paths and raw API JSON.
  Never upload recordings, frames or audio. Keep local and cloud clients separate.
- **Moment context.** Start a fresh request per moment, bounded by a decision
  timestamp. Exclude chat history, prior reviews, full-match dumps and later outcomes.
  Explicitly selected patch-relevant knowledge is allowed; unknowns remain unknown.
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
- [x] Windows setup: project venv/requests, FFmpeg/FFprobe, authenticated OBS control,
      loopback LM Studio, synthetic 4K/60 H.264 capture and local vision/text transport
- [x] CLI pipelines decoupled from the review UI via `review_data.py`; recorder CLI
      can check OBS, arm capture and link saved recordings
- [x] First Practice Tool recording: 188 s at 4K/60; user confirmed the full minimap
      after resizing. Local clock readings agree to whole seconds with the saved offset.
- [x] Second Practice Tool run: automatic stop confirmed by the user, saved metadata
      without errors, and full audio/video decode passed (232 s, 4K/60).
- [x] Human clock check: second recording at video 1:00 shows game 1:01, consistent
      with the saved -1.6 s offset at whole-second precision (one checkpoint).
- [ ] Record a normal match for Riot match linking and death-clip review

The review app was restored on 2026-10-02 after being deleted by mistake; it now
reuses `review_data.py`, which the CLI pipelines share.
Synthetic tests establish transport and schema checks, not coaching quality.

- [x] `fetch_matches.py`: Riot API → `league.db` (matches, frames, events, raw JSON)
- [x] `analyze.py`: matchups, backs, roams, death map
- [x] `review_app.py`: local review UI, moments, notes, focus, manual video sync
- [x] Step 0: DBs and IDE folders git-ignored and unstaged
- [x] `analyze.py charm`: Charm estimate = `enemyChampionImmobilizations / spell3Casts`
- [x] `analyze.py profile`: end-of-game habits per champion
- [x] Charm/profile bootstrap confidence intervals and review API endpoints
- [x] OBS recorder, recording links and local video serving (mocked tests; Windows capture unverified)
- [x] `coach/clips.py`: local death clips and a timestamped evidence manifest
- [x] `coach/harness.py`: fresh local observation/review requests with strict moment JSON
- [x] `coach/eval.py`: portable datasets, cached attempts, comparisons, human ratings and text-only Sol adapter (offline verified; live API unverified)
- [x] `coach/event_eval.py`: dense action timeline scoring, independent of sparse-frame caps

**Current direction (2026-10-02):** Windows setup and synthetic capture/model smoke
checks are complete. A first Practice Tool recording passed media checks; the user
confirmed its minimap is fully visible (the small local model incorrectly flagged
clipping). Manual OBS stop exposed a recorder error; the saved entry was recovered
and the recorder now retains its own output filename for that case. The second run
confirmed automatic stopping and passed a human clock check at one timestamp;
subsecond sync, long-match drift and coaching quality remain unverified. First focus
remains deaths and positioning: verify normal-match linking, then collect a small
set of death clips with human-checked observations. Keep dense
video models and training behind that milestone.

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
- [x] Confidence intervals on the Charm and profile numbers, bootstrapped per game
- [ ] Charm panel in the review app (per-game trend, W/L, opponent)
- [ ] Make **Today's focus** the centre: one goal, a measurable check (e.g. "Charm est.
      ≥ 55%" or "0 deaths before 10:00"), then graded automatically over the next 3–5 games
- [ ] Matchup card for champ select: your notes plus Charm and death stats vs that opponent
- [ ] Apply for a Riot Personal API Key

### Phase 2: Video v1 (auto-sync and death clips)
- [x] **`recorder.py` OBS auto-record (implemented; real Windows session verification pending)**
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
- [x] CLI death clip extraction: up to 60 s before and 10 s after, with evidence manifest
- [ ] Verify OBS capture, browser playback and clock sync on a real Windows recording
      (first session 2026-09-30; rehearsed on the Mac with a synthetic MKV whose frames show
      the game second: offset → clip → frame lined up to the second)
- [ ] Extend clip extraction to other flagged moments
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

### Phase 6: Moment coach (local, OpenAI or Claude)
- [x] Local observation/review harness with versioned JSON contracts and bounded context
- [ ] Validate vision and coaching outputs against real Windows footage; no model benchmark yet
- [ ] Benchmark a local coach against GPT-6.1 Sol using identical sanitised evidence
- [x] Separate OpenAI benchmark client with text approval, budget reservations and no automatic retries
- [ ] Live Sol reference sample (offline adapter verified; live model access unverified)
- [ ] Separate Claude client for sanitised text-only coaching
- [ ] Native-video/dense detector ingestion adapters and human-labelled real action timelines
- [ ] Blind human ratings: one sheet mixing runs under anonymous labels (per-run sheets reveal the model)
- [ ] Add pre-decision Riot timeline facts to packets (per-minute positions, level, gold,
      items, wards, objectives); today the coach sees only kill events and model observations
- [ ] Non-death control moments, so the coach can be checked for saying "this was fine"
- [ ] Output: cited, hypothesis-labelled review plus a proposed focus. You accept it into
      the `focus` table.

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
| 2. Clip captions | describe a flagged clip | Qwen3.8-27B candidate; compare smaller vision models and native-video runtimes on labelled moments (fit/speed unverified) | 5080 (+5060 Ti with explicit runtime splitting/offload) |
| 3. Coach | reason over one moment's JSON evidence and selected game knowledge | Local model, GPT-6.1 Sol or Claude | local or text-only cloud |

## Verification
- `python -m unittest -v` stays green, with tests alongside each new module
- Charm: the API estimate agrees with video-counted hits on labelled games
- Detectors: precision/recall on a held-out labelled set
- Win-prob: held-out Brier/log-loss and a calibration plot; top swings in 3 of your
  games match your memory or VOD
- Coach: spot-check 5 claims per report against the DB
- Practice loop: does the focus metric move over the following games?
