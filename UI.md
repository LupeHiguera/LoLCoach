# UI.md: LoLCoach design system

**Direction: a 2003–04 game-studio Flash interface, the "machined device" look, rebuilt
as a crisp, accessible HTML tool. League's Hextech art direction is a loose influence.**

Reference screenshots are in `Inspiration/` (local only, not committed): 2Advanced Studios v4 (2003), Conspiracy
Games (2004) and Xbox.com (2004). What we take from them: the page is a piece of
hardware (a chassis), each region is a labelled subsystem panel, title bars carry a `>>`
marker, machined trim is hatched, and data sits in recessed "screens". What we leave:
Flash intros, splash art, hazard stripes, fake LEDs and gauges, and tiny pixel type.

It should feel **hand-made and specific**, not a template with a sci-fi font.

## 1. Principles
1. **Period structure, modern execution.** Borrow the structure of the 2003 studio sites
   (chassis, subsystem panels, bevelled keys, recessed screens) and render it crisply,
   accessibly and responsively. No irony, no animation for show, no autoplay audio.
2. **Ornament has a job.** Bevels show what is clickable. Title bars label regions.
   Hatching is trim: it fills dead space in title bars, tabs and the footer, and nothing
   else. Recessed wells hold data. If an ornament doesn't do one of these, remove it.
3. **Data is the hero.** Tables and timelines get the space. Chrome stays thin.
4. **No fake hardware.** No pixel-font LED counters, blinking lights, dials, window
   controls that do nothing, or status lights that show nothing real. The period sites
   had them; on a tool they read as decoration and as the generic "vibe-coded" look.
5. **Inspired by League, not copied from it.** No Riot logos, no Beaufort (Riot's
   proprietary font), no screenshots of the client UI, and no 1:1 copies of client
   frames. Champion and item icons from Data Dragon are allowed, cached locally.

## 2. Page structure

```
┌═════════════════════════ chassis (max 1180px, centred) ══════════════════════════┐
║ HEADER PLATE  ▌LOLCOACH · session ticker                  [ 42 games imported ]  ║
║ TABS  [ REVIEW ][ CHARM ][ PROFILE ]  over hatched trim                          ║
╟───────────────┬──────────────────────────────────────────────────────────────────╢
║ SIDEBAR       │ CONTENT                                                          ║
║ >> FOCUS      │ >> AHRI VS ZOE ////////////////////// ▲W Victory                 ║
║ >> RECORDER   │    [ recessed chart screen ]                                     ║
║ >> GAMES ///  │ >> MOMENTS /////////////////////////                             ║
║   [Fetch new] │ >> KILLS ///////////////////////////                             ║
║               │ >> REVIEW  fieldset · recording                                  ║
╟───────────────┴──────────────────────────────────────────────────────────────────╢
║ FOOTER  [LOCAL ONLY][NO OVERLAYS][PYTHON STDLIB][NOT RIOT] · Riot notice         ║
║ STATUS LINE                                                                      ║
╚══════════════════════════════════════════════════════════════════════════════════╝
```

- **Body background:** a fine diagonal hatch on `--void` around the chassis.
- **Chassis:** the page frame, with a 6px bevelled steel border (light top/left, dark
  bottom/right), a 1px black outline and a hard 3px offset shadow.
- **Sidebar:** 240px, stacked panels, a real grid column that scrolls with the page.
- **Widths:** designed for 1280–1920px desktop. Below 1024px the sidebar stacks above
  the content. Below 640px the chassis border drops away. Not designed for phones, but
  it must not break on them.
- **Spacing:** 4px base, using 4 / 8 / 12 / 16 / 24 / 32. Dense: 8–12px inside panels.

## 3. Colour tokens
The materials are gunmetal steel with amber labels. Semantic colours carry meaning and
are never used as decoration.

```css
:root {
  /* materials */
  --void:      #08090b;  /* behind the chassis */
  --steel-900: #111418;  /* recessed wells: chart screens, inputs, zebra */
  --steel-800: #1a1e24;  /* panel body */
  --steel-700: #242a31;  /* raised: title bars, table header */
  --steel-600: #313840;  /* hover, bevel mid */
  --steel-400: #58616c;  /* chassis trim, cut edges */
  --steel-200: #aab3bd;  /* brushed highlight, reference lines, table headers */
  --amber-400: #e2b650;  /* structure labels: >> markers, logotype bar, active tab, legends */
  --amber-700: #6b5423;  /* rule under title bars */
  --arcane-400: #45b8b0; /* interaction: links, focus ring, primary button */
  --arcane-700: #1d5c5a; /* selected row */
  --text:      #e4e7eb;  /* primary text */
  --ash:       #9aa3ad;  /* secondary text */
  --ink-line:  #2e353e;  /* table grid, dividers */

  /* semantics: meaning only */
  --win:   #4fae7a;
  --loss:  #d0564d;
  --warn:  #e07b39;  /* orange, kept apart from the amber labels */
  --charm: #e0679a;  /* Ahri/Charm data only: hits, casts, Charm charts */
}
```

- Text contrast is at least WCAG AA. `--text` on `--steel-800` is about 13:1, `--ash`
  on `--steel-800` is about 6:1, and `--void` on `--arcane-400` (primary button) is
  about 8:1.
- **Amber labels structure. Arcane is interaction.** Never swap them, and never use
  amber for data, links or state. Amber stays rare: markers and active states, not
  body text.
- Win/loss always pair colour with a letter or glyph (W/L, ▲/▼) for colour-blind users.
- A champion accent (like `--charm`) is only used on that champion's data.

## 4. Materials and ornament
- **Bevels:** 1px highlight on the top/left (`--steel-200` at about 30% alpha) and 1px
  shadow on the bottom/right (`#000` at about 65%). Pressed and recessed invert them.
- **Title bars (subsystem labels):** a vertical 2-stop gradient (`--steel-600` →
  `--steel-700`), a 1px `--amber-700` rule below, an amber `>>` marker before the
  uppercase title, then a hatched strip filling the gap up to the right-aligned tools.
  Gradients are allowed only here, in the header plate, on tabs and on buttons.
- **Hatching:** 135° 1px lines in `--steel-400` at about 35% alpha, every 4px. Only in
  title-bar gaps, behind the tabs, and in the footer. Never behind data or text.
- **Recessed screens:** charts sit in a `--steel-900` well with an inverted bevel.
- **Chamfers:** major panels cut their top-left and bottom-right corners by 8px
  (`clip-path`), with the cut edge drawn in `--steel-400`. Don't chamfer small controls.
- **Radius:** 0 by default, 2px at most on inputs. No pills.
- **Rules:** a 1px `--steel-600` line with a 1px black shadow under it. One SVG divider
  is allowed at the top of each view, in `--steel-400`.
- **Shadows:** only the hard offset kind (`2px 2px 0 #000`) under raised panels. No blur,
  no glows.

## 5. Typography
No web fonts are shipped. Both faces are system fonts, which keeps the CSP at
`default-src 'self'`.

| Role | Face | Notes |
|---|---|---|
| Display (logotype, title bars, tabs, legends, plates) | **Bahnschrift** (ships with Windows 10/11) → DIN Alternate → Arial Narrow | Squared, industrial DIN, the machined-label feel. Weight 600, uppercase, normal letter-spacing. |
| Body and UI | **Verdana → Tahoma → system sans** | The 2000s web face, very legible at small sizes |
| Numbers | Body face with `font-variant-numeric: tabular-nums` | Every stat column |

- **No pixel fonts.** No Silkscreen, 04b or LED-digit faces anywhere, including for
  counters and labels.
- Type scale (px): **12 / 13 / 15 / 18 / 24 / 32**. Body text is 13px. The minimum for
  any information is 12px.
- Title bars use the display face at 15px, uppercase. Uppercase is for these structure
  labels only, never for sentences, data or small eyebrow text above headings.
- Links are underlined in `--arcane-400` and visited links in `--ash`.

## 6. Components
- **Panel (box):** title bar + body. The basic unit. Use one only for a separate
  region, and don't nest panels inside panels.
- **Nav tabs:** bevelled keys joined to the content edge. The active tab is recessed
  into the content colour, with amber text and a 3px amber bar along its top.
- **Buttons:** rectangular and bevelled, 13px bold, pressed state inverts the bevel.
  - Primary: arcane face with dark text.
  - Secondary: steel face.
  - No icon-only buttons without a label or `aria-label`.
- **Stat tables:** the workhorse.
  - 1px `--ink-line` grid and zebra rows (`--steel-800` / `--steel-900`).
  - Sticky header in `--steel-700` with `--steel-200` text, sortable with ▲/▼.
  - Numbers right-aligned with tabular figures; the sample size `n` is always visible.
- **Forms:** `<fieldset>` with an amber display-face `<legend>`, labels to the left of
  inputs where there's room, and inset inputs.
- **Session ticker:** one line in the header plate showing recent results, like
  `Last 10: ▲W ▼L ▼L ▲W · focus: 0 deaths before 10:00`. Static by default; it only
  scrolls when it overflows and `prefers-reduced-motion` is off.
- **Readout plate:** the header's games count, plain body text in a recessed well
  ("**42** games imported"). Never pixel digits or zero-padding.
- **Fetch new** (Games title bar): runs `fetch_matches.py` through `/api/fetch`.
  Disabled with "Fetching…" while it runs; progress and the result go to the status line.
- **Stamped plates** (footer): "LOCAL ONLY", "NO OVERLAYS", "PYTHON STDLIB", "NOT RIOT"
  in the display face on bevelled steel, each with a `title` explaining it.
- **Tooltips:** a small steel box with a `--steel-400` 1px border, shown on hover and
  focus. Used for definitions and caveats.
- **Status line** (replaces toasts): a strip at the bottom of the chassis showing "Saved
  22:41", fetch progress, or errors marked with a `--loss` bar. Nothing floats.

## 7. Charts
- Hand-written SVG in a recessed screen. Gridlines use `shape-rendering: crispEdges` in
  `--ink-line`, and axis text is 12px `--ash`.
- **Annotate events on the timeline:** deaths as a `--loss` ✕, the kill strip above the
  plot (team kills `--win`, enemy kills `--loss`, labelled rows), Charm hits as
  `--charm` ticks.
- **Label directly** on or next to the line. Only use a legend when there are more than 3 series.
- **Show uncertainty:** ranges drawn as a lighter band, with `n` next to every rate
  ("50% ±6, n=21").
- The zero line and reference lines are 1px `--steel-200`. Positive and negative areas
  may take a faint `--win`/`--loss` fill at 12% alpha.

## 8. Copy
- Use plain, specific labels ("Deaths before 10:00", "Charm est."). Title bars are
  nouns, and buttons are verbs ("Save review", "Fetch new").
- No fake system copy ("SYS.ONLINE", "MAINFRAME™", "SENT BACK IN TIME…") in the
  chrome or anywhere else.
- **Caveats:** each derived signal has one ⓘ tooltip holding its definition and limits
  (from the README). Don't repeat caveats in body text.
- **Empty states** say what's missing and the next action: "No timeline for this match.
  Run `fetch_matches.py`."

## 9. States and accessibility
- Every view designs loading (a skeleton of the table rows), empty, error, and partial
  data (no timeline, no opponent, no video, no kill list).
- Keyboard: all actions reachable, a visible `--arcane-400` 2px focus ring, and
  shortcuts for the review flow (J/K = previous/next moment, Space = play clip).
- `prefers-reduced-motion` disables the ticker scroll and all transitions. Transitions
  are ≤ 120ms otherwise.
- Semantic HTML: `<table>` for tables, `<fieldset>` for forms, landmarks for
  banner/nav/aside/main/footer.

## 10. Checklist before finishing UI work
- [ ] None of the vibe-coded tells in `AGENTS.md` were introduced
- [ ] Only tokens from §3; amber only for structure, arcane only for interaction,
      semantic colours only for their meaning
- [ ] Sizes from the §5 scale; nothing informational under 12px; numbers tabular; no pixel fonts
- [ ] Gradients only in title bars, header plate, tabs and buttons; hatching only as
      trim; radius ≤ 2px; no blur shadows
- [ ] No fake hardware (LEDs, dials, dead window controls) and no fake system copy
- [ ] Each derived number shows `n` and has one caveat tooltip
- [ ] Loading, empty, error and partial states exist
- [ ] Keyboard and focus ring work; reduced motion respected; contrast AA
- [ ] No web fonts or remote images (CSP is `default-src 'self'`)
- [ ] No Riot logos, Beaufort font, or copied client UI; Riot notice in footer
