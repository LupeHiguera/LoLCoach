# UI.md: LoLCoach design system

**Direction: an early-2000s gaming fansite, rebuilt with modern craft, using League's
Hextech art direction as inspiration.**

Picture a 2003 guild site or strategy portal: centered page frame, beveled title bars,
dense stat tables, sidebar boxes, fieldsets, 88×31 badges. Then add the materials of
Runeterra: dark lacquer, brass trim, arcane teal, angular chamfers and filigree. Build
it with today's standards for accessibility, crisp rendering and restraint.

It should feel **hand-made and specific**, not a template with a fantasy font.

## 1. Principles
1. **Period structure, modern execution.** Borrow the layout grammar of the 2000s web
   (frames, title bars, tables, fieldsets) but render it crisply, accessibly and
   responsively. Avoid irony and deliberate ugliness: no Comic Sans, no rainbow text,
   no `<marquee>`, no autoplay audio.
2. **Ornament has a job.** Bevels show what is clickable. Title bars label regions.
   Filigree marks the start of a major section. If an ornament doesn't do one of
   these, remove it.
3. **Data is the hero.** Tables and timelines get the space. Chrome stays thin.
4. **Inspired by League, not copied from it.** No Riot logos, no Beaufort (Riot's
   proprietary font), no screenshots of the client UI, and no 1:1 copies of client
   frames. Champion and item icons from Data Dragon are allowed, cached locally.

## 2. Page structure
The page frame follows the classic fixed-width portal.

```
┌──────────────────────── page frame (max 1180px, centered) ───────────────────────┐
│ BANNER  logotype · session ticker                         games reviewed 0042   │
│ NAV TABS  [ Review ][ Charm ][ Profile ][ Matchups ]                            │
├───────────────┬─────────────────────────────────────────────────────────────────┤
│ SIDEBAR       │ CONTENT                                                         │
│ ▣ Today's     │ ▣ Match header (title bar)                                      │
│   focus box   │   timeline chart                                                │
│ ▣ Games list  │ ▣ Moments table                                                 │
│   (filters)   │ ▣ Review fieldset  ·  clip player                               │
├───────────────┴─────────────────────────────────────────────────────────────────┤
│ FOOTER  88×31 badges · last updated · Riot notice                               │
└──────────────────────────────────────────────────────────────────────────────────┘
```

- **Body background:** a tiled texture (see §4) around the frame, like the old sites.
  The frame itself sits on solid colour.
- **Sidebar:** 240px, stacked boxes, like a frameset nav, but a real grid column that
  scrolls with the page.
- **Widths:** designed for 1280–1920px desktop. Below 1024px the sidebar stacks above
  the content. It is not designed for phones, but it must not break on them.
- **Spacing:** 4px base, using 4 / 8 / 12 / 16 / 24 / 32. The period look is **dense**,
  so default to 8–12px inside boxes.

## 3. Colour tokens
Base materials are dark lacquer and brass. Semantic colours carry meaning and are
never used as decoration.

```css
:root {
  /* materials */
  --void:        #07090d;  /* body behind the texture */
  --lacquer-900: #0d1219;  /* page frame */
  --lacquer-800: #131a24;  /* box body */
  --lacquer-700: #1b2431;  /* raised / table header */
  --lacquer-600: #263245;  /* hover row, bevel mid */
  --brass-500:   #b8965a;  /* trim, rules, title-bar text */
  --brass-300:   #dcc38c;  /* bevel highlight, active tab */
  --brass-700:   #6e5630;  /* bevel shadow */
  --arcane-400:  #45b8b0;  /* links, focus ring, interactive highlight */
  --arcane-700:  #1d5c5a;
  --parchment:   #e6dcc6;  /* primary text */
  --ash:         #9aa3ad;  /* secondary text */
  --ink-line:    #2c3647;  /* table grid, dividers */

  /* semantics: meaning only */
  --win:   #4fae7a;
  --loss:  #c4524a;
  --warn:  #d9a441;
  --charm: #e0679a;  /* Ahri/Charm data only: hits, casts, Charm charts */
}
```

- Text contrast is at least WCAG AA. `--parchment` on `--lacquer-800` is about 12:1,
  `--ash` on `--lacquer-800` is about 6:1.
- **Brass is for structure** (trim, rules, title bars). **Arcane is for interaction**
  (links, focus). Don't swap them.
- Win/loss always pair colour with a letter or glyph (W/L, ▲/▼) for colour-blind users.
- A champion accent (like `--charm`) is only used on that champion's data.

## 4. Materials and ornament
- **Body texture:** a small self-hosted SVG or PNG tile (≤ 4 KB) of subtle hex-lattice
  or stone noise, low contrast on `--void`. No photos, no splash art in the background.
- **Bevels:** 1px highlight on the top/left (`--brass-300` at about 35% alpha) and 1px
  shadow on the bottom/right (`#000` at about 60%). Pressed state inverts them.
- **Title bars:** a vertical 2-stop gradient (`--lacquer-600` → `--lacquer-700`), a
  1px brass rule below, title in the display face, and optional right-aligned tools
  (sort, collapse ▾). This is the only place gradients are allowed, apart from buttons.
- **Chamfers:** major boxes may cut their top-left and bottom-right corners by 6px
  (`clip-path: polygon(...)`), a nod to Hextech angularity. Don't chamfer small controls.
- **Radius:** 0 by default, 2px at most on inputs. No pills.
- **Rules:** a double hairline (`border-top: 3px double var(--brass-700)`) between
  sections. One SVG **filigree divider** is allowed at the top of each page or view.
- **Shadows:** only the hard offset kind (`2px 2px 0 #000`) under raised boxes. No blur glows.

## 5. Typography
Everything is self-hosted from `review_web/fonts/`, with OFL licenses kept alongside.

| Role | Face | Notes |
|---|---|---|
| Display (logotype, title bars, h1–h2) | **Marcellus SC** (OFL) | Carved, classical, small caps. The "Runeterra" feel without Riot's font. |
| Body and UI | **Verdana → Tahoma → system sans** | The authentic 2000s web face, and very legible at small sizes on Windows |
| Numbers | Body face with `font-variant-numeric: tabular-nums` | Every stat column |
| Pixel accent | **Silkscreen** (OFL) | Only for 88×31 badges and LED counters, at 8px multiples with `image-rendering: pixelated`-style crispness |

Type scale (px): **12 / 13 / 15 / 18 / 24 / 32**.
- Body text is 13px, which suits Verdana's large x-height. The minimum for any
  information is 12px.
- Title bars use Marcellus SC at 15px with normal letter-spacing.
- Links are underlined in `--arcane-400` and visited links in `--ash`. Visible
  underlines are part of the period style.

## 6. Components
- **Box (portal box):** title bar + body. The basic unit. Use one only for a separate
  region, and don't nest boxes inside boxes.
- **Nav tabs:** beveled tabs joined to the content edge. The active tab is raised in
  `--lacquer-800` with brass-300 text, and inactive tabs are recessed.
- **Buttons:** rectangular and beveled, 13px bold, with an inset look when pressed.
  - Primary: brass face with dark text.
  - Secondary: lacquer face.
  - No icon-only buttons without a label or `aria-label`.
- **Stat tables:** the workhorse.
  - 1px `--ink-line` grid and zebra rows (`--lacquer-800` / `--lacquer-900`).
  - Sticky header in `--lacquer-700`, sortable with ▲/▼.
  - Numbers right-aligned with tabular figures; the sample size `n` is always visible.
- **Forms:** `<fieldset>` with a `<legend>` in the display face, labels to the left of
  inputs where there's room, and inset inputs.
- **Session ticker:** one line in the banner showing recent results, like `W L L W ·
  focus: 0 deaths before 10:00 (2/3)`. Static by default; it only scrolls horizontally
  when it overflows and `prefers-reduced-motion` is off.
- **LED counter:** "games reviewed 0042" in Silkscreen on a recessed panel. A nod to
  the hit counter that shows real data.
- **88×31 badges** (footer): "LOCAL ONLY", "NO OVERLAYS", "PYTHON", "NOT RIOT". Pixel
  art, self-made.
- **Tooltips:** a small lacquer box with a brass 1px border, shown on hover and focus.
  Used for definitions and caveats.
- **Status line** (replaces toasts): a fixed strip at the bottom of the page frame,
  like a browser status bar, showing "Saved 22:41" or errors in `--loss`. Nothing floats.

## 7. Charts
- Hand-written SVG. Gridlines use `shape-rendering: crispEdges` in `--ink-line`, and
  axis text is 12px `--ash`.
- **Annotate events on the timeline:** deaths as a `--loss` ✕, Charm hits as `--charm`
  ticks, objectives as brass diamonds.
- **Label directly** on or next to the line. Only use a legend when there are more than 3 series.
- **Show uncertainty:** ranges drawn as a lighter band, with `n` next to every rate
  ("50% ±6, n=21").
- The zero line is a 1px brass rule. Positive and negative areas may take a faint
  `--win`/`--loss` fill at 12% alpha.

## 8. Copy
- Use plain, specific labels ("Deaths before 10:00", "Charm est."). Title bars are
  nouns, and buttons are verbs ("Save review").
- A little period flavour is fine only in the chrome (the badge text, "last updated").
  Never in data labels or feedback.
- **Caveats:** each derived signal has one ⓘ tooltip holding its definition and limits
  (from the README). Don't repeat caveats in body text.
- **Empty states** say what's missing and the next action: "No timeline for this match
  — run `fetch_matches.py`."

## 9. States and accessibility
- Every view designs loading (a skeleton of the table rows), empty, error, and partial
  data (no timeline, no opponent, no video).
- Keyboard: all actions reachable, a visible `--arcane-400` 2px focus ring, and
  shortcuts for the review flow (J/K = previous/next moment, Space = play clip).
- `prefers-reduced-motion` disables the ticker scroll and all transitions. Transitions
  are ≤ 120ms at most otherwise.
- Semantic HTML: `<table>` for tables, `<fieldset>` for forms, landmarks for
  banner/nav/aside/main/footer.

## 10. Checklist before finishing UI work
- [ ] None of the vibe-coded tells in `AGENTS.md` were introduced
- [ ] Only tokens from §3; semantic colours only for their meaning
- [ ] Sizes from the §5 scale; nothing informational under 12px; numbers tabular
- [ ] Gradients only in title bars and buttons; radius ≤ 2px; no blur shadows
- [ ] Each derived number shows `n` and has one caveat tooltip
- [ ] Loading, empty, error and partial states exist
- [ ] Keyboard and focus ring work; reduced motion respected; contrast AA
- [ ] Fonts and images are self-hosted (CSP is `default-src 'self'`)
- [ ] No Riot logos, Beaufort font, or copied client UI; Riot notice in footer
