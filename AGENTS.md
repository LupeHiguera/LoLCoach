# AGENTS.md

Instructions for coding agents working on LoLCoach. Read this first, then `PLAN.md` for
the roadmap. For any UI work, also read `UI.md`.

## Project
A personal, local-first, post-game coach for League of Legends (Ahri mid). It pulls the
user's matches from the Riot API into SQLite, analyses them, and runs a local review app.

| File | Role |
|---|---|
| `fetch_matches.py` | Riot API → `league.db` (`RiotClient`, `store_match`, `store_timeline`) |
| `analyze.py` | CLI analysis: matchups, backs, roams, deaths, charm, profile |
| `review_app.py`, `review_web/` | Local review app at http://127.0.0.1:8765 |
| `test_*.py` | unittest suites |
| `PLAN.md` | Roadmap and constraints |
| `UI.md` | Design system. Mandatory for UI work. |

## Commands
```
python -m unittest -v                 # all tests; must stay green
python review_app.py                  # review app on :8765
python analyze.py [section]           # read-only analysis, works with an expired key
python fetch_matches.py --count 20    # needs RIOT_API_KEY and RIOT_ID in .env
```

## Hard constraints
- **No ban risk.** Use official Riot APIs, the user's own recordings and the user's own
  replays only. Never add memory reading, input hooks or automation, overlays, or
  anything shown or spoken while a game is running. Champ select and post-game are fine.
- **Local-first.** Data, video and audio stay on this machine. Only the Claude coach
  calls the cloud, and it gets the user's own match data with other players' PUUIDs
  and names stripped.
- **Privacy.** Never commit `*.db`, `data/`, `.env`, recordings or audio. They hold
  API keys and other players' identifiers. Check `git status` before any commit.
- **Honest signals.** Derived numbers are review prompts, not diagnoses. Show sample
  size and uncertainty, cite the evidence (event, timestamp, stat), and never present a
  correlation as a cause.
- **Dependencies.** `review_app.py` and `review_web/` use the standard library and
  hand-written HTML/CSS/JS only, with no build step, frameworks or CDNs. The CSP is
  `default-src 'self'`, so fonts and images must be self-hosted. ML code goes in
  `coach/` with its own `requirements-ml.txt`.
- **Riot legal.** Keep the "not endorsed by Riot Games" notice. Don't use Riot logos or
  proprietary fonts, and don't copy the game client's UI. Take inspiration only (see `UI.md`).

## Code conventions
- Match the surrounding code: small functions, short docstrings that say *what the
  number means and doesn't mean*, and module constants for thresholds.
- `league.db` is opened read-only by analysis and the app. User-written data goes in
  `reviews.db`.
- Add tests next to the existing ones (`test_<module>.py`), and use in-memory SQLite
  built from `fetch_matches.SCHEMA`.

## UI: the "vibe-coded" look is banned
Follow `UI.md`. The patterns below are the generic look of AI-generated apps. Don't
introduce them, and remove them when you touch code that has them. A pattern is allowed
only when `UI.md` explicitly defines it as part of our style (for example the bevels
and title-bar gradients, which are period-specific and specified).

**Layout**
1. *Card soup*: every section in the same rounded, bordered, padded box, so there is no hierarchy.
2. *Landing-page hero inside a tool*: a marketing headline and tagline above the real work.
3. *Decorative step indicators* that do nothing (`01 / REVIEW → 02 / PRACTICE`).
4. *Pep-talk empty states* ("A small review goes a long way.") instead of saying what's missing and how to fix it.

**Typography**

5. *Eyebrow labels*: tiny uppercase letter-spaced accent text over every heading.
6. *Negative letter-spacing* on every heading.
7. *Information in text under 12px.*
8. *Ad hoc font sizes* (21, 25, 32, 43px) instead of the `UI.md` type scale.

**Colour and decoration**

9. *One neon accent on near-black* reused for links, buttons, wins, times and toasts until it means nothing.
10. *Soft generic gradients* on panels. Purple→blue "AI" gradients, glassmorphism, glows.
11. *Pill badges for everything.*
12. *Decorative glyphs*: "↗" on buttons that don't leave the page, fake "●" status dots, emoji or ✨ icons for "AI".
13. *Big soft drop shadows* and `border-radius` ≥ 12px as default styling.

**Copy**

14. *Coach-voice filler* ("Choose one thing you can control.") where a plain label belongs.
15. *Disclaimers repeated in body text.* Put caveats once, in the defined place (`UI.md` → Caveats).

**Interaction and data**

16. *Toasts for every event*, fixed at the bottom centre.
17. *Default-looking charts*: no annotations, no direct labels, axes with no meaning.
18. *Low density*: large padding, few items per screen, in a tool used for an hour at a time.
19. *Undesigned states*: missing loading, empty, error and partial-data states (no timeline, no video).

Before finishing UI work, check your change against this list and the `UI.md` checklist.
