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
  timeline data, a recording, or recollection. The UI does not run AI coaching;
  the optional recorder uses the local game-clock endpoint only to start/stop capture
  and estimate sync. A separate CLI model harness is described below.

Verify with `python -m unittest -v`.

## First video milestone: death clips

`coach/clips.py` prepares local footage for post-game analysis. It exports one MP4
per recorded death, with up to 60 seconds before and 10 seconds after it, plus a
`manifest.json` containing game/video timestamps and nearby Riot kill events.
The metadata excludes player names, PUUIDs and raw API JSON. Video itself is not
anonymised; keep the output local. Clip extraction does not run a model.

On the Windows gaming PC:

1. Configure an OBS scene using Window Capture for the game. Keep the full HUD,
   minimap and game clock visible, without resizing or cropping them. Start with
   native resolution at 60 fps and NVENC H.264; check a short test recording for
   legible HUD text and dropped frames before recording ranked games.
2. Save recordings under `data/recordings/`. OBS recommends MKV for recovery after
   interrupted recordings; remux to MP4 in OBS if the review browser cannot play it.
   See [OBS's recording guide](https://obsproject.com/kb/standard-recording-output-guide).
3. In OBS, enable the WebSocket server on port 4455, use a password, and put that
   password in `.env` as `OBS_WS_PASSWORD`. The existing recorder expects OBS at
   `C:\Program Files\obs-studio\bin\64bit\obs64.exe`.
4. Run `python review_app.py` and arm Auto-record before a match. Leave the app
   process running through the end. The recorder is off by default, polls only
   the local official game-clock endpoint, and displays no in-game advice.
5. After the match, use Fetch new with a valid Riot key, then open the match to
   link its saved recording. Check that Jump to review start agrees with the
   visible game clock. The current OBS clock offset is approximate.
   To check a clip: `death_clip_s` in the manifest is where the death should be. Pause
   there and read the game clock. If it differs from the death time (`death_game_ms`),
   the correct offset is `offset_s + (expected − shown)` in seconds, e.g. expecting
   5:00 and seeing 4:58 gives `offset_s + 2`. Rerun with `--video` and that `--offset`
   into a new `--output`.
6. Install [FFmpeg](https://ffmpeg.org/download.html) with both `ffmpeg` and
   `ffprobe` on PATH. These are external executables, not Python dependencies.
   Extract clips using the imported match ID:

```sh
python -m coach.clips --match NA1_123456789 --dry-run
python -m coach.clips --match NA1_123456789
```

On Windows, `py -m coach.clips` also works. Output goes to
`data/clips/NA1_123456789/`, which is git-ignored. Existing batches are preserved;
use another `--output` directory for a new sync or window. Failed extractions do
not publish a partial batch. Both databases are opened read-only.

For a finished recording that is not linked, pass the file and offset together:

```sh
python -m coach.clips --match NA1_123456789 --video data/recordings/game.mp4 --offset -95.5
```

The mapping is `video time = game time + offset`. If recording starts at game
1:35.5, the offset is **-95.5**. Clips missing some context are marked truncated;
deaths outside the recording are listed as skipped. The manifest marks sync as
unverified, even when it comes from OBS. Cuts, pauses or changing replay speeds
require a different mapping and are not supported by this first version.
`--before`, `--after`, `--db`, `--notes`, `--ffmpeg` and `--ffprobe` override defaults.
`--dry-run` still probes the video but writes no output.

The portable timing/privacy tests run without footage or a GPU. When FFmpeg is
installed, an additional integration test generates a synthetic video and checks
the extracted MP4 duration. Real OBS capture and sync still need verification on
the Windows PC before feeding clips into a video model.

## Local model harness: one decision at a time

`coach/harness.py` connects to a local model server's `/v1/chat/completions`
endpoint, using JSON-schema output and standard-library Python. LM Studio documents
[schema-constrained local output](https://lmstudio.ai/docs/developer/openai-compat/structured-output).
Load a vision-capable model for `observe`, and a text/reasoning model for `review`
(the same model can serve both passes). Pass the identifier shown by your server
with `--model`. No models are downloaded or loaded by this CLI.

Each call starts with exactly one system message and one user message. The server
may reuse its internal prompt cache, but no previous conversation, review or
match outcome is sent. Context defaults to **30 seconds before a chosen decision**,
with 6 frames. Caps are 60 seconds, 8 frames, 12 events, 4 knowledge snippets and
24,000 text characters including instructions/output schema. Frames are resized
to fit 1024×1024 during preparation, then capped at 1 MiB each and 1280×1280 pixels.
These limits bound workload; actual visual token usage depends on the model.
An oversized input is rejected, not silently truncated. Shorten `--window-ms`
or explicitly choose fewer frames/snippets when needed.

Try the synthetic preview now, without footage, FFmpeg, a model or credentials:

```sh
python -m coach.harness prepare --manifest coach/examples/clip_manifest.json --moment death-300000 --at-ms 295000 --output data/moments/example --dry-run
python -m coach.harness observe --packet coach/examples/packet.json --model LOCAL_VISION_MODEL --dry-run
python -m coach.harness review --packet coach/examples/packet.json --observations coach/examples/observations.json --model LOCAL_COACH_MODEL --dry-run
python -m coach.harness schema observations
```

The examples are fabricated contract fixtures, not gameplay evidence or measured
model outputs. Example frame filenames do not point to real images; model request
previews substitute image placeholders and never contact the server or write files.

Once a real recording has been clipped, select a decision **before** the death.
For example, `295000` means game 4:55, before a death at 5:00:

```sh
python -m coach.harness prepare --manifest data/clips/NA1_123456789/manifest.json --moment death-300000 --at-ms 295000 --output data/moments/decision-1
python -m coach.harness observe --packet data/moments/decision-1/packet.json --model LOCAL_VISION_MODEL --output data/moments/decision-1/observations.json
python -m coach.harness review --packet data/moments/decision-1/packet.json --observations data/moments/decision-1/observations.json --model LOCAL_COACH_MODEL --output data/moments/decision-1/review.json
```

Adjust the match ID, moment ID and timestamp to your recording. Preparation uses
FFmpeg to extract only frames at/before that decision. It publishes the bundle only
after every frame succeeds. All outputs preserve existing files; choose new paths
when rerunning. Local server default: `http://127.0.0.1:1234/v1`, overridable with
`--base-url`. Only literal loopback HTTP addresses are accepted. Proxies, redirects,
cloud URLs, credentials and automatic retries are disabled. Defaults are a 300 s
timeout and 8192 output tokens (`--timeout-s`, `--max-tokens`). Thinking models such
as Qwen3.8 reason at high effort by default and can exhaust smaller limits;
`--reasoning-effort low|medium|high` is passed through as the OpenAI-compatible field,
but whether your runtime honours it is unverified. Incomplete or invalid responses
are rejected before saving, with the `finish_reason` in the error.

JSON contracts and cross-reference checks live in `coach/contracts.py`:

- `packet.json`: opaque moment ID, patch/champions, bounded events/frames, sync
  status, focus and selected knowledge. No win/loss, match ID, raw API JSON or history.
- `observations.json`: visible statements, frame references/timestamps and unknowns.
  Every model statement is labelled `model_observed`; a model can never output
  `human_verified`. After checking a statement against the footage yourself, set
  `verification` to `human_verified` (or write your own with `source: "human"`).
  The review prompt treats only those as checked.
- `review.json`: cited observations/hypotheses, at most one alternative with its
  tradeoff, and a practice focus. `needs_more_evidence` requires missing evidence
  and cannot emit an alternative or practice focus.

For game knowledge, `prepare --knowledge selected-rules.json` accepts a JSON list
of up to four `{ "id": "rule-1", "patch": "16.18", "text": "..." }` objects.
Use the packet's exact patch or `general` for patch-independent principles.
Knowledge is explicitly selected, never an automatic dump of all game information.
Riot events are labelled global events; they do not establish what you could see.
Sparse frames can miss movements or mechanics, and unverified sync remains explicit.
Structural validation verifies references and timestamps, not whether a frame
actually supports a claim. Check initial outputs against the footage yourself.

The harness implements local calls only. The separate evaluation client described
below permits **sanitised text-only** OpenAI coaching. Recordings, frames and audio
stay local. A strict JSON schema cannot anonymise arbitrary free text.

## Model testing pipeline

`coach/eval.py` runs the same bounded moments through different models, saves each
attempt, and compares reports. Everything uses the standard library and works on
Mac or Windows. No model download or home-PC connection is needed to test the pipeline:

```sh
python -m unittest -v test_eval
python -m coach.eval validate --dataset coach/examples/eval_dataset.json
python -m coach.event_eval --gold coach/examples/events_gold.json --predictions coach/examples/events_predictions.json
```

The examples are **fabricated fixtures**, with no gameplay images. The tests exercise
real localhost image/text transport using tiny synthetic PNGs and simulate OpenAI
responses. Passing them verifies plumbing, not model coaching quality. No paid
requests are needed. The FFmpeg integration test in the full suite skips when FFmpeg
is unavailable.

There are three separate evaluations:

| Evaluation | Input | What is measured |
|---|---|---|
| Coaching (`--task review`, default) | Fixed packet and observations, identical for every coach | Contract/citation validity, expected assessment, timing, human coaching rubric |
| Sparse vision smoke test (`--task observe`, local only) | Current harness's selected PNG frames | Contract/timestamp checks and human vision rubric |
| Dense action evaluation (`coach.event_eval`) | Human-labelled and predicted event timelines | One-to-one action precision/recall/F1 and timestamp error |

The sparse path is capped at eight frames and sixteen observations. It **cannot
evaluate hundreds of actions in a clip**. Dense action scoring accepts up to 10,000
actions per bounded moment, independently of those caps. It needs a future detector
or native-video adapter to produce the predictions; this change does not implement
dense video ingestion. Use canonical labels such as `charm_cast` and stable actor
aliases such as `self` / `enemy-mid`; prose descriptions are not automatically
converted into action labels. The example action timelines are synthetic too.

For real tests, put a dataset and its prepared moment bundles under `data/evals/`.
Copy the structure of `coach/examples/eval_dataset.json`: each case has a unique
`id`, an opaque game `group`, `split` (`dev` or `test`), relative `packet` and
`observations` paths, nullable `expected_assessment`, and nullable
`cloud_approved_sha256`. Keep every moment from one game in the same split. Labels
and expected assessments never enter model input. Keep test games out of prompt
tuning. Use human-checked evidence (`human_verified`) first to isolate coaching; compare ingestion
quality separately before allowing its errors to contaminate the coach comparison.

With a local OpenAI-compatible runtime already serving a downloaded model:

```sh
python -m coach.eval run --dataset data/evals/moments/dataset.json --model LOCAL_MODEL_A --output data/evals/local-a --runtime-info coach/examples/home_runtime.json --reasoning-effort low
python -m coach.eval run --dataset data/evals/moments/dataset.json --model LOCAL_MODEL_B --output data/evals/local-b --runtime-info coach/examples/home_runtime.json
python -m coach.eval compare --dataset data/evals/moments/dataset.json --runs data/evals/local-a data/evals/local-b
python -m coach.eval rating-sheet --dataset data/evals/moments/dataset.json --run data/evals/local-a --output data/evals/local-a/ratings.json
python -m coach.eval score-ratings --run data/evals/local-a --ratings data/evals/local-a/ratings.json
```

Replace the example runtime fields with actual runtime/model revision, quantization,
context size, GPU split and CPU offload settings. The home PC has 96GB system RAM
and two 16GB GPUs; record how the runtime uses them instead of treating VRAM as
automatically pooled. Record cold/warm runs explicitly. Latency is end-to-end wall
time, not GPU utilization or tokens/second. Use a new output directory for an
intentional repeat; resuming the same directory skips every already attempted
case, including failures and pending requests with uncertain billing. A changed
prompt, model, evidence or runtime configuration requires another directory.

Each run saves `run.json`, hashed per-case result files and `report.json`. Reports
show case/game counts, failures, assessment agreement, median latency, usage and
estimated cloud cost. Rates use finished attempts only (`not_run` and `pending` are
excluded) and include a 95% Wilson interval; with a few dozen cases these are wide,
so small differences between models are noise. Contract pass rates and assessment agreement are screening
checks, **not factual accuracy**. Fill all five scores in `ratings.json` with
`0` (incorrect/absent), `1` (partial), or `2` (sound), and count unsupported claims.
The coaching rubric covers evidence, positioning, feasible advice, uncertainty and
practice usefulness. The vision rubric covers visible facts, timestamps, action
coverage, uncertainty and citations. `compare` includes completed human ratings
when a run has `ratings.json`. Different valid coaches need not give identical advice.

GPT-6.1 Sol is a **coaching reference**, not ground truth or a cloud video reviewer.
The explicit OpenAI adapter uses the Responses API with fresh context, strict JSON
output, low reasoning effort, no tools and `store:false`. Only the exact sanitized
text is eligible; images/audio/video are never sent. Obvious paths, Riot IDs,
PUUID markers, URLs and secrets are rejected, but arbitrary player names still
require human review. Before any future paid run:

1. Finish secure key setup in the ignored `.env.local` (`OPENAI_API_KEY`), or use
   the configured environment key. Never paste a key into a command or dataset.
2. Run `preview-cloud` for each case, inspect every text field, remove identifiers,
   and copy the printed hash into that case's `cloud_approved_sha256`. Any text
   edit invalidates the hash. This is a privacy check, not verification of truth.
3. Choose an explicit total run budget and a small `--limit`. A paid run is opt-in:

```sh
python -m coach.eval preview-cloud --dataset data/evals/moments/dataset.json --case CASE_ID
# Only after approving the text and choosing to spend:
python -m coach.eval run --dataset data/evals/moments/dataset.json --provider openai --model gpt-6.1-sol --limit 2 --budget-usd 1 --output data/evals/sol-reference
```

The automated tests make no paid calls. The client makes no
automatic retries, reserves conservative input/max-output cost before each request,
and stops before the next reservation exceeds the chosen budget. Failed or pending
attempts keep their reservation. Prices are a dated estimate, not an invoice or a
provider-enforced spend limit; use Platform limits too. Reasoning tokens are already
included in billed output usage. Responses with missing usage have unknown cost.
`store:false` disables response storage; it is not a zero-retention guarantee.
The API is fixed to OpenAI HTTPS and does not use proxy/redirect or custom base URLs.

Official API references: [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol),
[Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs),
[data controls](https://developers.openai.com/api/docs/guides/your-data).

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
