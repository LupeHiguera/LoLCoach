# Synthetic moment fixtures

These files exercise the evaluation contracts. They are fabricated. They are not
gameplay evidence, not human review of a recording, and not a model score.
`human_verified` here means the JSON flag is set so the schema can be checked.
It does not mean someone watched a game.

Frame filenames match the packet contract. This directory does not contain the
PNG files. `validate` does not open images. An observe run reads PNGs from the
packet's directory and fails if they are missing.

## What is in the set

`eval_dataset.json` (`synthetic-moment-fixtures`, `synthetic: true`) indexes
every moment. The first two test cases are the original pair, in that order, so
`--limit 2` still smokes only those two. Later cases add plumbing edges.

| Case | Group | Split | `expected_assessment` | What it adds |
|---|---|---|---|---|
| `missing-footage` | `synthetic-game-1` | test | `needs_more_evidence` | Frames, no observations |
| `visible-positioning` | `synthetic-game-2` | test | `reviewable` | One `model_observed` note |
| `same-game-reset` | `synthetic-game-2` | test | `reviewable` | Second moment of that game, same split; a two-frame citation and a `human` / `human_verified` note |
| `unlabelled-trade` | `synthetic-game-3` | test | `null` | No screening label; `sync_verified: false`; a `general` knowledge snippet |
| `event-without-view` | `synthetic-game-4` | test | `needs_more_evidence` | A death timeline row and frames, with no visual observation |
| `checked-retreat` | `synthetic-game-5` | test | `reviewable` | Mixed verification, a two-frame citation, a kill row with an assist, patch-matched knowledge |
| `dev-opening` | `synthetic-game-dev` | dev | `reviewable` | Dev-split moment |
| `dev-followup` | `synthetic-game-dev` | dev | `null` | Later moment of that same dev game |
| `dev-window-bounds` | `synthetic-game-dev-edges` | dev | `null` | Exact 60s window, null patch/champion/opponent, null kill fields, `general` knowledge |

`expected_assessment` is `reviewable`, `needs_more_evidence`, or `null`. The key
is required. `null` means unlabelled, not a third verdict. Omitting the key is
invalid. Labels, group, split, paths, and `cloud_approved_sha256` are not model
input.

One group uses one split. `same-game-reset` stays on `test` with
`visible-positioning`. `dev-followup` stays on `dev` with `dev-opening`.
`validate` rejects a group that appears in both.

`event-without-view` is the observe/review contrast the schema allows. There is
no per-case task field. Observe and review are run flags. Every case names at
least one frame, so observe is not rejected for an empty frame list; the PNG
still has to exist beside the packet. Review also sees timeline events.
`event-without-view` has a death row and no observation: the screening label is
`needs_more_evidence` because nothing describes the picture. A model may cite
the death row and return `reviewable`; that disagrees with this label. Observe
requests drop timeline events, so that row is not in the vision prompt.

Champion names in these files are roles in the fiction (`Ahri`, `Syndra`), not
player names. No Riot ID, PUUID, match id, local path, or key belongs here.

`events_gold.json` and `events_predictions.json` are the small action example
(unchanged). `events_gold_edges.json` and `events_predictions_edges.json` add,
for the same scorer and the default 250 ms tolerance:

- actions on the first and last millisecond of the window
- a second prediction of one hit
- the same label on a different actor
- a move 1000 ms away
- one prediction next to two attacks 200 ms apart
- a predicted ward with no gold ward

They are synthetic too. Scoring them does not read video.

## Adding real labelled moments

Keep real moments on this machine, under `data/evals/`. That directory is
git-ignored with the rest of `data/`. Do not copy footage, frames, audio, or a
database into git. Do not add real moments under `coach/examples/`.

1. Clip your own recording, then build one bundle:
   `python -m coach.harness prepare --manifest data/clips/.../manifest.json --moment MOMENT --at-ms DECISION_MS --output data/evals/moments/MOMENT`.
   Pick a decision at or before the moment you want reviewed. The packet must
   not carry a match id, player name, Riot ID, PUUID, local path, or raw API JSON.
2. Optional: `python -m coach.harness observe` into that bundle. Leave
   `verification` as `model_observed`.
3. Watch the frames yourself. Set `verification` to `human_verified` only for
   statements you checked. A note you write yourself uses `source: human`,
   which must be `human_verified`. Unchecked notes stay `model_observed`.
4. Add a case next to those bundles in a dataset file under `data/evals/`:
   - unique `id`
   - opaque `group` for the game, not a match id or a player name
   - `split`: `dev` while you are still changing prompts; `test` once you are
     scoring. Every moment from one game stays in one split. Do not move a
     test game back to `dev` to tune a prompt.
   - relative `packet` and `observations` paths with forward slashes
   - `expected_assessment`: `null` until you choose `reviewable` or
     `needs_more_evidence`
   - `cloud_approved_sha256`: `null` until you run `preview-cloud`, read every
     text field, and copy that exact hash. An edit invalidates it.
   - `"synthetic": false` on a dataset of real moments
5. `python -m coach.eval validate --dataset data/evals/.../dataset.json`

Dense action labels for `coach.event_eval` follow the same privacy rule and
also stay under `data/evals/`. Use canonical labels and actor aliases. Prose is
not converted into labels.

`validate` checks structure, paths, and cross-references. A green run does not
mean a model was right, that the clock was synced, or that a person watched a
game. Assessment agreement is a screening check. Rubric scores in `ratings.json`
are a separate human judgment.
