'use strict';
/* LoLCoach review app. Hand-written, no build step. Design system: UI.md. */

// ---------------------------------------------------------------- helpers
const $ = id => document.getElementById(id);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const clock = ms => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;
const fmt = n => Number(n).toLocaleString('en-US');
const signed = n => n == null ? 'n/a' : `${n > 0 ? '+' : n < 0 ? '−' : ''}${fmt(Math.abs(n))}`;
const day = ms => new Date(ms).toLocaleDateString(undefined, {month: '2-digit', day: '2-digit'});
const isoDay = ms => { const d = new Date(ms); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`; };
const hhmm = () => new Date().toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
const QUEUES = {420: 'Ranked solo/duo', 440: 'Ranked flex', 400: 'Normal draft'};
const METRICS = {gold_diff: 'Gold', xp_diff: 'XP', cs_diff: 'Lane CS'};
const EVIDENCE = {stats_only: 'timeline only', recording: 'recording', memory: 'recollection'};

/** Win/loss always pairs colour with a glyph and a letter (UI.md §3). */
const result = win => win
  ? '<span class="res win"><span class="g" aria-hidden="true">▲</span>W</span>'
  : '<span class="res loss"><span class="g" aria-hidden="true">▼</span>L</span>';

const skeleton = (cols, rows = 6) =>
  Array.from({length: rows}, () => `<tr class="skeleton">${'<td><span></span></td>'.repeat(cols)}</tr>`).join('');
const emptyRow = (cols, html) => `<tr class="empty-row"><td colspan="${cols}">${html}</td></tr>`;

const state = {
  matches: [], detail: null, moment: null, metric: 'gold_diff', killFilter: 'all',
  dirty: false, focusDirty: false, focus: null, videoURL: null, request: 0,
  view: 'review', loaded: {}, charm: null, profile: null,
};

// ---------------------------------------------------------------- data source
// Local app: /api/* from review_app.py. Static demo (<html data-mode="demo">, built by
// deploy_demo.ps1): read-only demo/data/*.json with the same shapes (API.md, "Demo snapshot").
// ?mock reads review_web/mock/*.json the same way, to preview the demo locally.
const DATA_BASE = document.documentElement.dataset.mode === 'demo' ? 'data/'
  : new URLSearchParams(location.search).has('mock') ? 'mock/' : null;
const READ_ONLY = DATA_BASE !== null;
const DATA_FILES = {'/api/matches': 'matches.json', '/api/focus': 'focus.json', '/api/charm': 'charm.json',
  '/api/profile': 'profile.json', '/api/recorder': 'recorder.json'};

/** The snapshot file standing in for an /api path, or undefined if the demo has none. */
function dataFile(path) {
  const url = new URL(path, location.href);
  if (url.pathname === '/api/match') return `match/${encodeURIComponent(url.searchParams.get('id') || '')}.json`;
  return DATA_FILES[url.pathname];
}

async function staticData(path, body) {
  if (body !== undefined) throw Error('Read-only demo: nothing is saved.');
  const file = dataFile(path);
  let r = null;
  if (file) try { r = await fetch(DATA_BASE + file); } catch (e) { throw Error(`Cannot load ${DATA_BASE}${file}.`); }
  if (!r || !r.ok) {
    const err = Error(path.startsWith('/api/match?') ? 'Match not found' : `${DATA_BASE}${file || path} is missing from this snapshot.`);
    err.status = 404; throw err;
  }
  return r.json();
}

async function api(path, body) {
  if (READ_ONLY) return staticData(path, body);
  const init = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)};
  let r;
  try { r = await fetch(path, init); } catch (e) {
    throw Error('Cannot reach the review server. Start it with python review_app.py, then reload.');
  }
  let data = null;
  try { data = await r.json(); } catch (e) { /* non-JSON error page */ }
  if (!r.ok) { const err = Error((data && data.error) || `Request failed (${r.status})`); err.status = r.status; throw err; }
  return data;
}

// ---------------------------------------------------------------- status line (replaces toasts)
function status(message, kind = 'ok') {
  const el = $('status');
  el.textContent = message;
  el.className = kind;
  el.title = message;
}

// ---------------------------------------------------------------- banner
function renderBanner() {
  const recent = state.matches.slice(0, 10);
  const results = recent.map(m => result(m.win)).join(' ');
  const focus = state.focus && state.focus.goal ? ` · focus: ${esc(state.focus.goal)}` : '';
  $('ticker-track').innerHTML = recent.length ? `Last ${recent.length}: ${results}${focus}` : `No games imported${focus}`;
  const ticker = $('ticker');
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  ticker.classList.remove('scrolling');
  if (!reduce && ticker.scrollWidth > ticker.clientWidth + 1) ticker.classList.add('scrolling');

  // "games reviewed" needs a reviewed flag per match; until the API sends one, count imports.
  const hasFlag = state.matches.some(m => 'reviewed' in m);
  const count = hasFlag ? state.matches.filter(m => m.reviewed).length : state.matches.length;
  $('led-label').textContent = hasFlag ? 'games reviewed' : 'games imported';
  $('led').textContent = String(count).padStart(4, '0');
  const latest = state.matches.reduce((a, m) => Math.max(a, m.game_start_ms || 0), 0);
  $('last-updated').textContent = `Latest game: ${latest ? isoDay(latest) : '—'}`;
}

// ---------------------------------------------------------------- games list
function filteredMatches() {
  const c = $('champion').value, q = $('queue').value, w = $('result').value;
  return state.matches.filter(m => (!c || m.my_champion === c) && (!q || String(m.queue_id) === q) && (!w || String(Number(m.win)) === w));
}

function renderMatches() {
  const list = filteredMatches();
  $('match-count').textContent = `n=${list.length}`;
  const active = state.detail && state.detail.match.match_id;
  $('matches').innerHTML = list.map(m => `
    <tr data-id="${esc(m.match_id)}" class="${m.match_id === active ? 'selected' : ''}">
      <td class="num">${day(m.game_start_ms)}</td>
      <td><button class="linkbtn" type="button" data-id="${esc(m.match_id)}" aria-current="${m.match_id === active}">${esc(m.my_champion)} v ${esc(m.opp_champion || '?')}</button></td>
      <td>${result(m.win)}</td>
      <td class="num">${clock(m.duration_s * 1000)}</td>
    </tr>`).join('') || emptyRow(4, state.matches.length
      ? 'No games match these filters. Change Queue or Result.'
      : 'No Ahri/Zoe mid games with a timeline yet. Run <code>python fetch_matches.py --count 20</code>.');
}

$('matches').onclick = e => { const row = e.target.closest('tr[data-id]'); if (row) openInReview(row.dataset.id); };
for (const id of ['champion', 'queue', 'result']) $(id).onchange = renderMatches;

// ---------------------------------------------------------------- match
function discardReview() { return !state.dirty || confirm('Leave this unsaved review?'); }

function showReviewEmpty(html, error = false) {
  $('detail').hidden = true;
  $('review-empty').hidden = false;
  $('review-empty-text').className = error ? 'state error' : 'state';
  $('review-empty-text').innerHTML = html;
}

async function loadMatch(id) {
  if (!discardReview()) return;
  const request = ++state.request;
  status(`Loading ${id}…`, 'note');
  try {
    const detail = await api('/api/match?id=' + encodeURIComponent(id));
    if (request !== state.request) return;
    state.detail = detail; state.moment = null; state.dirty = false;
    clearVideo(); showLinkedRecording();
    $('review-empty').hidden = true; $('detail').hidden = false;
    renderMatchHeader(); renderMatches(); renderChart(); renderMoments(); renderKills(); renderReviewForm();
    status(`Loaded ${detail.match.my_champion} vs ${detail.match.opp_champion || 'unknown'} · ${isoDay(detail.match.game_start_ms)}`, 'note');
  } catch (e) {
    if (request === state.request) status(e.message, 'error');
  }
}

function renderMatchHeader() {
  const m = state.detail.match;
  $('match-title').textContent = `${m.my_champion} vs ${m.opp_champion || 'unknown opponent'}`;
  $('match-result').innerHTML = `${result(m.win)} ${m.win ? 'Victory' : 'Defeat'}`;
  $('match-meta').textContent = [isoDay(m.game_start_ms), QUEUES[m.queue_id] || `Queue ${m.queue_id}`, `Patch ${m.patch}`, clock(m.duration_s * 1000)].join(' · ');
  const s = state.detail.score;
  const parts = [];
  if (s && s.kills != null) parts.push(`K/D/A ${s.kills} / ${s.deaths} / ${s.assists}`);
  if (s && s.team_kills != null) parts.push(`Team kills ${s.team_kills}–${s.enemy_kills} (timeline)`);
  $('match-score').textContent = parts.join(' · ');
  $('match-score').hidden = !parts.length;
}

// ---------------------------------------------------------------- timeline chart
/** Round step for gridlines: 1, 2 or 5 × 10^k, giving about `target` lines per side. */
function niceStep(max, target = 2) {
  const raw = max / target, p = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map(k => k * p).find(s => s >= raw);
}

/** Value at time t by linear interpolation between snapshots; null when it would bridge a gap. */
function valueAt(frames, key, t, cadence) {
  for (let i = 1; i < frames.length; i++) {
    const a = frames[i - 1], b = frames[i];
    if (a.time <= t && t <= b.time) {
      if (!Number.isFinite(a[key]) || !Number.isFinite(b[key]) || b.time - a.time > cadence * 1.25) return null;
      return a[key] + (b[key] - a[key]) * (t - a.time) / (b.time - a.time || 1);
    }
  }
  return null;
}

function renderChart() {
  const d = state.detail, key = state.metric, label = METRICS[key], box = $('chart');
  const frames = d.frames || [];
  if (!frames.length) {
    box.innerHTML = '<div class="state"><strong>No timeline for this match.</strong> Run <code>python fetch_matches.py</code> to import it; moments need a timeline too.</div>';
    $('chart-readout').textContent = '';
    return;
  }
  const points = frames.filter(f => Number.isFinite(f[key]));
  if (!points.length) {
    box.innerHTML = d.match.opp_champion
      ? `<div class="state"><strong>No ${label} comparison in these snapshots.</strong> The lane opponent's frames are missing.</div>`
      : '<div class="state"><strong>No lane opponent recorded for this match,</strong> so Gold, XP and CS differences are unavailable. Deaths are still listed in Moments.</div>';
    $('chart-readout').textContent = '';
    return;
  }
  const kills = d.kills || [];
  const W = 860, L = 56, R = 112, S = kills.length ? 36 : 0, T = 14 + S, B = 214 + S, H = 250 + S;
  const duration = Math.max(d.match.duration_s * 1000, ...frames.map(f => f.time), 60000);
  const peak = Math.max(key === 'cs_diff' ? 10 : 300, ...points.map(f => Math.abs(f[key])));
  const step = niceStep(peak), limit = Math.ceil(peak / step) * step;
  const x = t => +(L + (W - L - R) * t / duration).toFixed(1);
  const y = v => +(T + (B - T) * (1 - v / limit) / 2).toFixed(1);

  const segments = [];
  let seg = null, prev = null;
  for (const f of frames) {
    if (!Number.isFinite(f[key])) { seg = null; prev = null; continue; }
    if (!seg || f.time - prev.time > d.cadence_ms * 1.25) { seg = []; segments.push(seg); }
    seg.push(f); prev = f;
  }
  const pts = s => s.map(f => `${x(f.time)},${y(f[key])}`).join('L');
  const line = segments.map(s => 'M' + pts(s)).join('');
  const area = segments.map(s => `M${x(s[0].time)},${y(0)}L${pts(s)}L${x(s[s.length - 1].time)},${y(0)}Z`).join('');

  let svg = `<svg viewBox="0 0 ${W} ${H}" role="group" aria-label="${label} difference over the game. Focus a snapshot to read it; press Enter to review it.">`;
  svg += `<defs><clipPath id="clip-pos"><rect x="0" y="0" width="${W}" height="${y(0)}"/></clipPath><clipPath id="clip-neg"><rect x="0" y="${y(0)}" width="${W}" height="${H - y(0)}"/></clipPath></defs>`;
  for (let t = 0; t <= duration; t += 300000) {
    svg += `<line class="grid" x1="${x(t)}" x2="${x(t)}" y1="${T}" y2="${B}"/><text class="axis" x="${x(t)}" y="${B + 18}" text-anchor="middle">${clock(t)}</text>`;
  }
  for (let v = -limit; v <= limit; v += step) {
    svg += `<line class="${v === 0 ? 'zero' : 'grid'}" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${signed(v)}</text>`;
  }
  svg += `<text class="axis" x="${W - R}" y="${B + 34}" text-anchor="end">game time (min)</text>`;
  if (state.moment) {
    const x0 = x(state.moment.start_ms), x1 = x(state.moment.end_ms);
    svg += `<rect class="band" x="${x0}" y="${T}" width="${Math.max(3, x1 - x0)}" height="${B - T}"/>`;
  }
  svg += `<path class="area-pos" d="${area}" clip-path="url(#clip-pos)"/><path class="area-neg" d="${area}" clip-path="url(#clip-neg)"/>`;
  svg += `<path class="line" d="${line}"/>`;
  if (kills.length) svg += killStrip(kills, x, L, W - R);
  for (const ts of d.deaths || []) {
    const v = valueAt(frames, key, ts, d.cadence_ms);
    svg += `<text class="death" x="${x(ts)}" y="${y(v ?? 0) + 5}" text-anchor="middle">✕<title>Your death at ${clock(ts)}</title></text>`;
  }
  const last = points[points.length - 1];
  svg += `<text class="direct" x="${x(last.time) + 8}" y="${y(last[key]) + 4}">${label} ${signed(last[key])}</text>`;
  points.forEach((f, i) => {
    svg += `<circle class="pt" cx="${x(f.time)}" cy="${y(f[key])}" r="3" tabindex="0" role="button" data-index="${i}" aria-label="${clock(f.time)}, ${label} ${signed(f[key])}. Review this snapshot."><title>${clock(f.time)}: ${label} ${signed(f[key])}</title></circle>`;
  });
  box.innerHTML = svg + '</svg>';

  const idle = () => { $('chart-readout').textContent = state.moment ? `Selected ${clock(state.moment.start_ms)}–${clock(state.moment.end_ms)} · ${state.moment.title}` : 'Hover or focus a snapshot to read it; click to review it.'; };
  idle();
  box.querySelectorAll('.pt').forEach(p => {
    const f = points[Number(p.dataset.index)];
    const read = () => { $('chart-readout').textContent = `${clock(f.time)} · Gold ${signed(f.gold_diff)} · XP ${signed(f.xp_diff)} · Lane CS ${signed(f.cs_diff)}`; };
    const pick = () => selectMoment({id: `custom-${f.time}`, start_ms: f.time, end_ms: f.time, kind: 'custom',
      title: `Snapshot at ${clock(f.time)}`, description: 'Chosen from the timeline.'});
    p.onmouseenter = read; p.onfocus = read; p.onmouseleave = idle;
    p.onclick = pick;
    p.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
  });
}

document.querySelectorAll('[data-metric]').forEach(b => b.onclick = () => {
  state.metric = b.dataset.metric;
  document.querySelectorAll('[data-metric]').forEach(t => t.setAttribute('aria-pressed', String(t === b)));
  if (state.detail) renderChart();
});

/** Two rows of ticks above the plot: kills by my team, then kills by the enemy team. Thick = I took part. */
function killStrip(kills, x, left, right) {
  let svg = '';
  [['ally', 'Team', 10], ['enemy', 'Enemy', 28]].forEach(([side, label, y0]) => {
    svg += `<text class="axis" x="${left - 6}" y="${y0 + 8}" text-anchor="end">${label}</text>`;
    svg += `<line class="grid" x1="${left}" x2="${right}" y1="${y0 + 10}" y2="${y0 + 10}"/>`;
    for (const k of kills.filter(k => k.side === side)) {
      svg += `<line class="kill-${side}${k.me ? ' kill-mine' : ''}" x1="${x(k.time)}" x2="${x(k.time)}" y1="${y0}" y2="${y0 + 10}"><title>${clock(k.time)} ${esc(killLabel(k))}${k.me ? ` (your ${k.me})` : ''}</title></line>`;
    }
    const n = kills.filter(k => k.side === side).length;
    svg += `<text class="direct" x="${right + 8}" y="${y0 + 9}">${n} kills</text>`;
  });
  return svg;
}

// ---------------------------------------------------------------- kills (timeline champion kills)
const ROLE = {kill: 'Kill', death: 'Died', assist: 'Assist'};
/** Which team got the kill; colour paired with a glyph and a word (UI.md §3). */
const SIDE = {ally: '<span class="res win"><span class="g" aria-hidden="true">▲</span>Team</span>',
  enemy: '<span class="res loss"><span class="g" aria-hidden="true">▼</span>Enemy</span>'};
const killLabel = k => `${k.killer || 'Executed'} → ${k.victim || '?'}`;

/** The 30 s before a kill, as a moment for the review form. */
const killMoment = k => ({id: `kill-${k.time}`, start_ms: Math.max(0, k.time - 30000), end_ms: k.time, kind: 'kill',
  title: killLabel(k), description: 'The 30 seconds before this kill event.'});

function renderKills() {
  const d = state.detail, body = $('kills');
  document.querySelectorAll('[data-kills]').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.kills === state.killFilter)));
  if (!Array.isArray(d.kills)) {
    $('kill-count').textContent = '';
    body.innerHTML = emptyRow(5, READ_ONLY ? 'This snapshot has no kill list. Rebuild it with <code>python export_demo.py</code>.'
      : 'This review_app.py sends no kill list. Update it and restart it, then reload.');
    return;
  }
  const mine = d.kills.filter(k => k.me);
  const list = state.killFilter === 'me' ? mine : d.kills;
  $('kill-count').textContent = `n=${d.kills.length} · with you ${mine.length}`;
  state.killRows = list;
  body.innerHTML = list.map((k, i) => `<tr data-index="${i}" class="${state.moment && state.moment.id === `kill-${k.time}` ? 'selected' : ''}">
      <td class="num">${clock(k.time)}</td>
      <td>${SIDE[k.side] || '<span class="cell-note">?</span>'}</td>
      <td><button class="linkbtn" type="button">${esc(killLabel(k))}</button></td>
      <td class="cell-note">${k.assists.length ? esc(k.assists.join(', ')) : '—'}</td>
      <td>${k.me ? `<span class="you">${ROLE[k.me]}</span>` : '<span class="cell-note">—</span>'}</td>
    </tr>`).join('') || emptyRow(5, !d.frames.length
      ? 'No timeline, so no kill list. Run <code>python fetch_matches.py</code> to import it.'
      : d.kills.length ? 'You took part in no kills this game. Choose All to see every kill.'
      : 'No champion kills in this timeline.');
}

$('kills').onclick = e => {
  const row = e.target.closest('tr[data-index]');
  if (row) selectMoment(killMoment(state.killRows[Number(row.dataset.index)]));
};
document.querySelectorAll('[data-kills]').forEach(b => b.onclick = () => {
  state.killFilter = b.dataset.kills;
  if (state.detail) renderKills();
});

// ---------------------------------------------------------------- moments (auto prompts + saved snapshots)
function momentRows() {
  const d = state.detail, rows = [...d.moments];
  for (const r of d.reviews) {
    if (rows.some(m => m.id === r.moment_id)) continue;
    const kill = (d.kills || []).find(k => `kill-${k.time}` === r.moment_id);
    if (kill) rows.push(killMoment(kill));
    else {
      rows.push({id: r.moment_id, start_ms: r.start_ms, end_ms: r.end_ms, kind: 'custom',
        title: r.start_ms === r.end_ms ? `Snapshot at ${clock(r.start_ms)}` : 'Saved moment', description: 'Chosen from the timeline.'});
    }
  }
  return rows.sort((a, b) => a.end_ms - b.end_ms || a.start_ms - b.start_ms);
}

const BASIS = {death: 'event timestamp', deficit: 'sampled snapshot', farm: 'sampled snapshots', kill: 'kill event', custom: 'your pick'};

function renderMoments() {
  const d = state.detail, rows = momentRows();
  state.rows = rows;
  $('moment-count').textContent = `${d.moments.length} prompts · ${d.reviews.length} saved`;
  $('moments').innerHTML = rows.map((m, i) => {
    const r = d.reviews.find(x => x.moment_id === m.id);
    const span = m.start_ms === m.end_ms ? clock(m.end_ms) : `${clock(m.start_ms)}–${clock(m.end_ms)}`;
    return `<tr data-index="${i}" class="${state.moment && state.moment.id === m.id ? 'selected' : ''}">
      <td class="num">${span}</td>
      <td><button class="linkbtn" type="button">${esc(m.title)}</button></td>
      <td class="cell-note">${BASIS[m.kind] || esc(m.kind)}</td>
      <td>${r ? `${esc(r.reason)} · ${esc(EVIDENCE[r.evidence] || r.evidence)}<br><span class="cell-note">${esc(r.observation.slice(0, 90))}${r.observation.length > 90 ? '…' : ''}</span>` : '<span class="cell-note">—</span>'}</td>
    </tr>`;
  }).join('') || emptyRow(4, d.frames.length
    ? 'No automatic prompts in this game. Click a point on the chart to review your own moment.'
    : 'No timeline, so no prompts. Run <code>python fetch_matches.py</code> to import it.');
}

$('moments').onclick = e => {
  const row = e.target.closest('tr[data-index]');
  if (row) selectMoment(state.rows[Number(row.dataset.index)]);
};

// ---------------------------------------------------------------- review form
function renderReviewForm() {
  const m = state.moment;
  $('review-none').hidden = !!m;
  $('review-form').hidden = !m;
  if (!m) { $('saved').textContent = ''; return; }
  const span = m.start_ms === m.end_ms ? clock(m.end_ms) : `${clock(m.start_ms)}–${clock(m.end_ms)}`;
  $('review-title').textContent = `${span} · ${m.title}`;
  $('review-description').textContent = m.description || '';
}

function selectMoment(moment) {
  if (!moment || !discardReview()) return;
  state.moment = moment; state.dirty = false;
  const review = state.detail.reviews.find(r => r.moment_id === moment.id);
  $('review-form').reset();
  if (review) for (const k of ['reason', 'evidence', 'observation', 'alternative']) $(k).value = review[k] ?? '';
  renderReviewForm();
  $('saved').textContent = review ? `Saved ${review.updated_at ? new Date(review.updated_at).toLocaleString() : ''}` : 'Not saved';
  renderMoments(); renderKills(); renderChart();
}

$('review-form').oninput = () => { state.dirty = true; $('saved').textContent = 'Unsaved changes'; };
$('review-form').onsubmit = async e => {
  e.preventDefault();
  if (!state.moment) return;
  const button = $('save-review'); button.disabled = true;
  const id = state.detail.match.match_id, moment = state.moment;
  const body = {match_id: id, moment_id: moment.id, start_ms: moment.start_ms, end_ms: moment.end_ms};
  for (const k of ['reason', 'evidence', 'observation', 'alternative']) body[k] = $(k).value;
  try {
    await api('/api/reviews', body);
    if (state.detail.match.match_id === id && state.moment && state.moment.id === moment.id) { state.dirty = false; $('saved').textContent = `Saved ${hhmm()}`; }
    status(`Review saved ${hhmm()}`);
    const fresh = await api('/api/match?id=' + encodeURIComponent(id));
    if (state.detail.match.match_id === id) { state.detail.reviews = fresh.reviews; renderMoments(); }
  } catch (err) {
    status(`Review not saved: ${err.message}`, 'error');
  } finally { button.disabled = false; }
};

$('use-focus').onclick = () => {
  if (state.focusDirty && !confirm('Replace the unsaved focus draft?')) return;
  const m = state.detail.match;
  openFocusForm();
  $('goal').value = $('alternative').value.slice(0, 1000);
  $('why').value = `${m.my_champion} vs ${m.opp_champion || '?'}, ${clock(state.moment.start_ms)}: ${$('observation').value}`.slice(0, 1000);
  $('check').value = '';
  state.focusDirty = true;
  $('goal').focus();
};

// ---------------------------------------------------------------- focus
function showFocus(f) {
  state.focus = f;
  $('focus-goal').textContent = f.goal || 'No focus set.';
  $('focus-why').textContent = f.goal ? (f.why_text || '') : 'Press Edit, or use "Use as focus" on a saved review.';
  $('focus-check').textContent = f.check_text ? `Check: ${f.check_text}` : '';
  $('goal').value = f.goal || ''; $('why').value = f.why_text || ''; $('check').value = f.check_text || '';
  renderBanner();
}
function openFocusForm() { $('focus-form').hidden = false; $('edit-focus').hidden = true; }
function closeFocusForm() { $('focus-form').hidden = true; $('edit-focus').hidden = false; state.focusDirty = false; }

$('edit-focus').onclick = () => { openFocusForm(); $('goal').focus(); };
$('cancel-focus').onclick = () => {
  if (state.focusDirty && !confirm('Discard the unsaved focus changes?')) return;
  showFocus(state.focus || {goal: '', why_text: '', check_text: ''});
  closeFocusForm();
};
$('focus-form').oninput = () => { state.focusDirty = true; };
$('focus-form').onsubmit = async e => {
  e.preventDefault();
  const button = $('save-focus'); button.disabled = true;
  try {
    showFocus(await api('/api/focus', {goal: $('goal').value, why_text: $('why').value, check_text: $('check').value}));
    closeFocusForm();
    status(`Focus saved ${hhmm()}`);
  } catch (err) {
    status(`Focus not saved: ${err.message}`, 'error');
  } finally { button.disabled = false; }
};

// ---------------------------------------------------------------- video (local file, never uploaded)
function clearVideo() {
  if (state.videoURL) URL.revokeObjectURL(state.videoURL);
  state.videoURL = null;
  const v = $('video'); v.pause(); v.removeAttribute('src'); v.load(); v.hidden = true;
  $('video-file').value = ''; $('video-offset').value = '0'; $('video-status').textContent = '';
}

/** Load the OBS recording linked to this match (API.md: match.recording), else explain what is missing. */
function showLinkedRecording() {
  const rec = state.detail.recording, note = $('recording-note');
  note.className = 'dim';
  if (!rec) { note.textContent = 'No recording linked. Choose a local file; it stays in this browser.'; return; }
  const name = String(rec.path || '').split(/[\\/]/).pop();
  if (!rec.url) {
    note.className = 'state error';
    note.textContent = `Linked recording ${name || ''} is no longer on disk. Choose the file if it moved.`;
    return;
  }
  $('video').src = rec.url; $('video').hidden = false;
  if (Number.isFinite(rec.offset_s)) $('video-offset').value = String(rec.offset_s);
  note.textContent = `Linked recording: ${name}. ` + (Number.isFinite(rec.offset_s)
    ? 'Offset read from the game clock.' : 'Game clock not read; set the offset by hand.');
}

$('video-file').onchange = () => {
  const file = $('video-file').files[0];
  if (!file) return;
  if (state.videoURL) URL.revokeObjectURL(state.videoURL);
  state.videoURL = URL.createObjectURL(file);
  $('video').src = state.videoURL; $('video').hidden = false;
  $('video-status').textContent = 'Recording loaded. Check that the offset lines up the game clock.';
};
$('video').onerror = () => { if ($('video').getAttribute('src')) $('video-status').textContent = 'The browser cannot play this format. Use an MP4 or WebM recording.'; };
$('seek-video').onclick = () => {
  const v = $('video'), offset = Number($('video-offset').value);
  if (!state.moment) { status('Pick a moment before jumping in the recording.', 'error'); return; }
  if (!v.src || !Number.isFinite(v.duration)) { status('Choose a playable recording first.', 'error'); return; }
  if (!Number.isFinite(offset)) { status('Enter the video time at game 0:00 in seconds.', 'error'); return; }
  const target = state.moment.start_ms / 1000 + offset;
  if (target < 0 || target > v.duration) { status(`${clock(state.moment.start_ms)} falls outside this recording. Check the offset.`, 'error'); return; }
  v.currentTime = target;
  $('video-status').textContent = `Video at ${clock(target * 1000)} = game ${clock(state.moment.start_ms)}.`;
};

const viewLoaders = {};  // view name -> function run each time the view is shown

// ---------------------------------------------------------------- view load errors
/** Show a view's error state in place of its boxes. A 404 means review_app.py predates the endpoint. */
function showViewState(view, e) {
  document.querySelectorAll(`#view-${view} > .box.major, #view-${view} > .split`).forEach(el => { el.hidden = true; });
  $(`${view}-missing`).hidden = false;
  const box = $(`${view}-missing-text`);
  const old = e.status === 404 && !READ_ONLY;
  box.className = 'state error';
  box.innerHTML = old
    ? `<strong>No ${view} data.</strong> This review_app.py has no /api/${view}. Update it, restart it, then <button class="btn" type="button" data-retry="${view}">Retry</button>`
    : `<strong>Could not load ${view} stats.</strong> ${esc(e.message)} <button class="btn" type="button" data-retry="${view}">Retry</button>`;
  status(`${view[0].toUpperCase() + view.slice(1)}: ${e.message}`, 'error');
}
function hideViewState(view) {
  document.querySelectorAll(`#view-${view} > .box.major, #view-${view} > .split`).forEach(el => { el.hidden = false; });
  $(`${view}-missing`).hidden = true;
}
document.addEventListener('click', e => {
  const b = e.target.closest('[data-retry]');
  if (b) { state.loaded[b.dataset.retry] = null; viewLoaders[b.dataset.retry](); }
});

// ---------------------------------------------------------------- sortable stat tables
function setupSort(theadId, sort, rerender) {
  $(theadId).querySelectorAll('th[data-sort]').forEach(th => {
    th.innerHTML = `<button type="button" class="sortbtn">${th.innerHTML}<span class="arrow" aria-hidden="true"></span></button>`;
    th.querySelector('button').onclick = () => {
      if (sort.key === th.dataset.sort) sort.dir = -sort.dir;
      else { sort.key = th.dataset.sort; sort.dir = th.dataset.dir === 'asc' ? 1 : -1; }
      rerender();
    };
  });
}
function sortRows(theadId, rows, sort) {
  $(theadId).querySelectorAll('th[data-sort]').forEach(th => {
    const on = th.dataset.sort === sort.key;
    if (on) th.setAttribute('aria-sort', sort.dir > 0 ? 'ascending' : 'descending'); else th.removeAttribute('aria-sort');
    th.querySelector('.arrow').textContent = on ? (sort.dir > 0 ? '▲' : '▼') : '';
  });
  return [...rows].sort((a, b) => {
    const va = a[sort.key], vb = b[sort.key];
    if (va == null) return vb == null ? 0 : 1;
    if (vb == null) return -1;
    return (typeof va === 'string' ? va.localeCompare(vb) : va - vb) * sort.dir;
  });
}

// ---------------------------------------------------------------- charm
const LOW_CASTS = 20;   // fewer E casts than this: one game's rate is noisy, drawn hollow
const ROLL = 5;         // rolling window (games) for the per-game trend line
const pct = v => v == null || !Number.isFinite(v) ? '—' : `${Math.round(v * 100)}%`;
const range = ci => ci ? `${Math.round(ci[0] * 100)}–${Math.round(ci[1] * 100)}%` : '—';
const charmSort = {key: 'start', dir: -1}, oppSort = {key: 'n', dir: -1};

/** Label ranges with the level the API used (method.ci_level), e.g. "90% range". */
function showCiLevel(method) {
  const level = method && Number.isFinite(method.ci_level) ? `${Math.round(method.ci_level * 100)}%` : null;
  document.querySelectorAll('[data-ci-label]').forEach(el => { el.textContent = level ? `${level} range` : 'Range'; });
  document.querySelectorAll('[data-ci-level]').forEach(el => {
    el.textContent = level ? `${level} bootstrap` : 'bootstrap';
    if (method && method.iterations) el.title = `${method.iterations} resamples of ${method.resample || 'game'}s`;
  });
  return level;
}

/** Dot = estimate, band = 95% range, brass tick = reference (all games). Coordinates in % so it fits any cell. */
function ciBar(rate, ci, lo, hi, ref) {
  const p = v => `${((Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo) * 100).toFixed(2)}%`;
  let s = '<svg class="ci" aria-hidden="true"><line class="track" x1="0" x2="100%" y1="8" y2="8"/>';
  if (ref != null) s += `<line class="ref" x1="${p(ref)}" x2="${p(ref)}" y1="1" y2="15"/>`;
  if (ci) s += `<rect class="range" x="${p(ci[0])}" width="${((Math.min(hi, ci[1]) - Math.max(lo, ci[0])) / (hi - lo) * 100).toFixed(2)}%" y="3" height="10"/>`;
  if (rate != null) s += `<circle class="dot" cx="${p(rate)}" cy="8" r="4"/>`;
  return s + '</svg>';
}

viewLoaders.charm = () => { if (!state.loaded.charm) state.loaded.charm = loadCharm(); };
async function loadCharm() {
  hideViewState('charm');
  $('charm-summary').innerHTML = skeleton(5, 3);
  $('charm-games').innerHTML = skeleton(8);
  $('charm-opp').innerHTML = skeleton(4);
  $('charm-trend').innerHTML = '<div class="state">Loading Charm stats…</div>';
  try {
    state.charm = await api('/api/charm');
    renderCharm();
  } catch (e) { showViewState('charm', e); }
}

function renderCharm() {
  const c = state.charm, games = c.games || [], s = c.summary || {};
  showCiLevel(c.method);
  $('charm-n').textContent = `n=${s.all ? s.all.n : games.length} games`;
  if (!games.length) {
    const msg = 'No Ahri games with E-cast stats stored. Import games with <code>python fetch_matches.py --count 20</code>.';
    $('charm-summary').innerHTML = emptyRow(5, msg);
    $('charm-trend').innerHTML = '';
    $('charm-games').innerHTML = emptyRow(8, 'No games.');
    $('charm-opp').innerHTML = emptyRow(4, 'No games.');
    return;
  }
  const rows = [['All games', s.all], ['Wins', s.wins], ['Losses', s.losses]];
  const vals = rows.flatMap(([, r]) => r ? [r.rate, ...(r.ci || [])] : []).filter(Number.isFinite);
  const lo = Math.max(0, Math.floor((Math.min(...vals) - .05) * 10) / 10);
  const hi = Math.min(1, Math.ceil((Math.max(...vals) + .05) * 10) / 10);
  $('charm-scale').innerHTML = `<span>${pct(lo)}</span><span>brass tick = all games</span><span>${pct(hi)}</span>`;
  $('charm-summary').innerHTML = rows.map(([label, r]) => r
    ? `<tr><th scope="row">${label}</th><td class="num">${r.n}</td><td class="num charm-rate">${pct(r.rate)}</td>
        <td class="num">${r.ci ? range(r.ci) : '<span class="cell-note">none, n &lt; 2</span>'}</td>
        <td>${ciBar(r.rate, r.ci, lo, hi, label === 'All games' || !s.all ? null : s.all.rate)}</td></tr>`
    : `<tr><th scope="row">${label}</th><td colspan="4" class="cell-note">Not in the response.</td></tr>`).join('');
  renderCharmTrend(games, s.all);
  renderCharmGames();
  renderCharmOpponents();
}

function renderCharmTrend(games, all) {
  const list = games.filter(g => Number.isFinite(g.rate)).sort((a, b) => a.start - b.start);
  const box = $('charm-trend');
  if (list.length < 2) { box.innerHTML = '<div class="state">The per-game trend needs at least 2 games with E casts.</div>'; return; }
  const W = 860, H = 232, L = 48, R = 176, T = 22, B = 180;
  const x = i => +(L + (W - L - R) * i / (list.length - 1)).toFixed(1);
  const y = v => +(T + (B - T) * (1 - v)).toFixed(1);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Charm estimate per game, oldest to newest, with a ${ROLL}-game rolling rate and the all-games rate">`;
  svg += `<text class="axis" x="${L}" y="12">Charm est. per game (oldest → newest) · hollow = under ${LOW_CASTS} casts</text>`;
  for (const v of [0, .25, .5, .75, 1]) svg += `<line class="${v === 0 ? 'zero' : 'grid'}" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${pct(v)}</text>`;
  let labelAll = null;
  if (all && Number.isFinite(all.rate)) {
    if (all.ci) svg += `<rect class="overall-band" x="${L}" width="${W - R - L}" y="${y(all.ci[1])}" height="${y(all.ci[0]) - y(all.ci[1])}"/>`;
    svg += `<line class="overall" x1="${L}" x2="${W - R}" y1="${y(all.rate)}" y2="${y(all.rate)}"/>`;
    labelAll = y(all.rate);
  }
  const roll = [];
  for (let i = ROLL - 1; i < list.length; i++) {
    const win = list.slice(i - ROLL + 1, i + 1), casts = win.reduce((a, g) => a + g.casts, 0);
    if (casts) roll.push([i, win.reduce((a, g) => a + g.hits, 0) / casts]);
  }
  if (roll.length > 1) svg += `<path class="roll" d="M${roll.map(([i, v]) => `${x(i)},${y(v)}`).join('L')}"/>`;
  list.forEach((g, i) => {
    svg += `<circle class="pt${g.casts < LOW_CASTS ? ' hollow' : ''}" cx="${x(i)}" cy="${y(g.rate)}" r="4"><title>${isoDay(g.start)} vs ${esc(g.opponent || '?')} · ${g.win ? 'W' : 'L'} · ${g.hits}/${g.casts} = ${pct(g.rate)}</title></circle>`;
    svg += `<text class="${g.win ? 'win' : 'loss'}" x="${x(i)}" y="${B + 16}" text-anchor="middle">${g.win ? '▲' : '▼'}<title>${g.win ? 'Win' : 'Loss'}</title></text>`;
    if (i % 5 === 0 || i === list.length - 1) svg += `<text class="axis" x="${x(i)}" y="${B + 34}" text-anchor="middle">${isoDay(g.start).slice(5)}</text>`;
  });
  svg += `<text class="axis" x="${W - R + 8}" y="${B + 16}">▲ win ▼ loss</text>`;
  // direct labels at the right edge, nudged apart when they would overlap
  let labelRoll = roll.length ? y(roll[roll.length - 1][1]) : null;
  if (labelAll != null && labelRoll != null && Math.abs(labelAll - labelRoll) < 28) {
    if (labelRoll <= labelAll) labelRoll = labelAll - 28; else labelRoll = labelAll + 28;
  }
  if (labelAll != null) svg += `<text class="label-brass" x="${W - R + 8}" y="${labelAll + 4}">All games ${pct(all.rate)}</text><text class="axis" x="${W - R + 8}" y="${labelAll + 18}">${all.ci ? range(all.ci) : 'no range'} · n=${all.n}</text>`;
  if (labelRoll != null) svg += `<text class="label-charm" x="${W - R + 8}" y="${labelRoll + 4}">Last ${ROLL} games ${pct(roll[roll.length - 1][1])}</text>`;
  box.innerHTML = svg + '</svg>';
}

function reviewCell(id) {
  return state.matches.some(m => m.match_id === id)
    ? `<button class="linkbtn" type="button" data-open="${esc(id)}">Open</button>`
    : '<span class="cell-note" title="Not in the review list: no timeline stored for this game">no timeline</span>';
}

function renderCharmGames() {
  const games = state.charm.games || [];
  $('charm-games-n').textContent = `n=${games.length}`;
  $('charm-games').innerHTML = sortRows('charm-games-head', games, charmSort).map(g => `<tr>
    <td class="num" title="${isoDay(g.start)}">${isoDay(g.start).slice(5)}</td><td>${esc(g.opponent || '?')}</td><td>${result(g.win)}</td>
    <td class="num${g.casts < LOW_CASTS ? ' cell-note' : ''}"${g.casts < LOW_CASTS ? ` title="Under ${LOW_CASTS} casts: noisy"` : ''}>${g.casts ?? '—'}</td>
    <td class="num">${g.hits ?? '—'}</td><td class="num charm-rate">${pct(g.rate)}</td>
    <td class="num">${Number.isFinite(g.per_min) ? g.per_min.toFixed(2) : '—'}</td><td>${reviewCell(g.match_id)}</td></tr>`).join('');
}

function renderCharmOpponents() {
  const opps = state.charm.by_opponent || [];
  $('charm-opp-n').textContent = `${opps.length} opponents`;
  $('charm-opp').innerHTML = sortRows('charm-opp-head', opps, oppSort).map(o => `<tr>
    <td>${esc(o.opponent || '?')}</td><td class="num">${o.n}</td><td class="num charm-rate">${pct(o.rate)}</td>
    <td class="num">${o.ci ? range(o.ci) : '<span class="cell-note" title="No range with fewer than 2 games">n &lt; 2</span>'}</td></tr>`).join('')
    || emptyRow(4, 'No opponents in the response.');
}

setupSort('charm-games-head', charmSort, renderCharmGames);
setupSort('charm-opp-head', oppSort, renderCharmOpponents);
$('charm-games').onclick = e => { const b = e.target.closest('[data-open]'); if (b) openInReview(b.dataset.open); };

// ---------------------------------------------------------------- profile
const num = v => Number.isFinite(v) ? v.toLocaleString('en-US', {minimumFractionDigits: 1, maximumFractionDigits: 2}) : '—';
const SMALL_N = 3;  // champions with fewer games are dimmed, as the CLI hides them
const UNITS = {ratio: '', per_10m: 'per 10 min', per_min: 'per min'};
const STAT_LABELS = {
  deaths_per_10m: 'Deaths', kill_participation: 'Kill participation', damage_share: 'Team damage share',
  damage_taken_share: 'Team damage taken share', vision_per_min: 'Vision score', time_dead_share: 'Time spent dead',
};
/** `ratio` values are 0–1 and shown as %. */
const statValue = (v, unit) => unit === 'ratio' ? pct(v) : num(v);
const statRange = (ci, unit) => ci ? `${statValue(ci[0], unit)}–${statValue(ci[1], unit)}` : null;

viewLoaders.profile = () => { if (!state.loaded.profile) state.loaded.profile = loadProfile(); };
async function loadProfile() {
  hideViewState('profile');
  $('profile-head').innerHTML = '';
  $('profile-body').innerHTML = skeleton(4, 6);
  try {
    state.profile = await api('/api/profile');
    renderProfile();
  } catch (e) { showViewState('profile', e); }
}

/** One row per champion on a shared scale: bar = 95% range, dot = mean. Ahri in her accent colour. */
function compareRows(key, champs) {
  const items = champs.map(c => ({c, s: (c.stats || []).find(s => s.key === key)})).filter(i => i.s && Number.isFinite(i.s.value));
  if (!items.length) return '';
  const vals = items.flatMap(i => [i.s.value, ...(i.s.ci || [])]);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * .08 || Math.abs(hi) * .1 || 1;
  lo = lo >= 0 ? Math.max(0, lo - pad) : lo - pad; hi += pad;  // these stats are never negative
  const W = 300, X0 = 64, X1 = 292, rowH = 16, H = rowH * items.length + 18;
  const x = v => +(X0 + (X1 - X0) * (v - lo) / (hi - lo)).toFixed(1);
  const unit = items[0].s.unit;  // one stat per row, so one unit
  let svg = `<svg class="cmp-plot" viewBox="0 0 ${W} ${H}" aria-hidden="true">`;
  items.forEach(({c, s}, i) => {
    const yy = 9 + i * rowH;
    svg += `<g class="${c.champion === 'Ahri' ? 'ahri' : ''}${c.n < SMALL_N ? ' small-n' : ''}"><text class="name" x="0" y="${yy + 4}">${esc(c.champion)}</text><line class="track" x1="${X0}" x2="${X1}" y1="${yy}" y2="${yy}"/>`;
    if (s.ci) svg += `<line class="range" x1="${x(s.ci[0])}" x2="${Math.max(x(s.ci[1]), x(s.ci[0]) + 1)}" y1="${yy}" y2="${yy}"/>`;
    svg += `<circle class="dot" cx="${x(s.value)}" cy="${yy}" r="3.5"/></g>`;
  });
  svg += `<text class="axis" x="${X0}" y="${H - 2}">${statValue(lo, unit)}</text><text class="axis" x="${X1}" y="${H - 2}" text-anchor="end">${statValue(hi, unit)}</text>`;
  return svg + '</svg>';
}

function renderProfile() {
  const champs = [...(state.profile.champions || [])].sort((a, b) => b.n - a.n);
  const level = showCiLevel(state.profile.method);
  if (!champs.length) {
    $('profile-n').textContent = 'n=0';
    $('profile-head').innerHTML = '<tr><th scope="col">Stat</th></tr>';
    $('profile-body').innerHTML = emptyRow(1, 'No games stored. Import games with <code>python fetch_matches.py --count 20</code>.');
    return;
  }
  $('profile-n').textContent = `${champs.length} champions · n=${champs.reduce((a, c) => a + c.n, 0)} games`;
  $('profile-head').innerHTML = `<tr><th scope="col">Stat</th>${champs.map(c => `<th scope="col" class="num${c.n < SMALL_N ? ' small-n' : ''}">${esc(c.champion)} · n=${c.n}${Number.isFinite(c.wins) ? `<br><span class="cell-note">${c.wins}W ${c.n - c.wins}L</span>` : ''}</th>`).join('')}<th scope="col" class="cmp">${level || 'Bootstrap'} ranges, shared scale per row</th></tr>`;
  const keys = [];
  for (const c of champs) for (const s of c.stats || []) if (!keys.some(k => k.key === s.key)) keys.push(s);
  $('profile-body').innerHTML = keys.map(k => `<tr><th scope="row">${esc(STAT_LABELS[k.key] || k.label)}${UNITS[k.unit] ? ` <span class="cell-note">${UNITS[k.unit]}</span>` : ''}</th>${champs.map(c => {
    const s = (c.stats || []).find(x => x.key === k.key);
    if (!s || !Number.isFinite(s.value)) return `<td class="num cell-note"${c.n < SMALL_N ? ' data-small' : ''}>—</td>`;
    const n = Number.isFinite(s.n) ? s.n : c.n;
    const r = statRange(s.ci, s.unit);
    const note = [r || `no range, n=${n}`, n !== c.n && r ? `n=${n}` : ''].filter(Boolean).join(' · ');
    return `<td class="num${c.n < SMALL_N ? ' small-n' : ''}">${statValue(s.value, s.unit)}<br><span class="cell-note">${note}</span></td>`;
  }).join('')}<td class="cmp">${compareRows(k.key, champs)}</td></tr>`).join('');
}

// ---------------------------------------------------------------- recorder (OBS auto-record, API.md /api/recorder)
const REC_POLL_MS = 5000;  // refresh while armed or recording; never polls when idle and off
const OBS_TEXT = {running: 'OBS connected', stopped: 'OBS not running', unavailable: 'OBS not set up'};
const REC_STATUS = {recording: 'recording now', saved: 'saved, links after the next fetch', linked: 'linked', failed: 'failed'};
let recTimer = null;

function renderRecorder(r) {
  const box = $('rec-armed');
  box.checked = !!r.armed;
  box.disabled = READ_ONLY;
  const game = r.game === 'in_game' ? 'game running, recording' : 'waiting for a game';
  const line = $('rec-line');
  if (READ_ONLY) { line.className = 'rec-line'; line.textContent = 'Off in the demo.'; $('rec-last').textContent = ''; return; }
  line.className = 'rec-line' + (r.error || r.obs === 'unavailable' ? ' error' : '');
  line.textContent = [r.armed ? `Armed · ${game}` : 'Off', OBS_TEXT[r.obs] || r.obs, r.error].filter(Boolean).join(' · ');
  const last = r.last;
  if (!last) { $('rec-last').textContent = 'No recordings yet.'; return; }
  const when = last.started_at ? new Date(last.started_at).toLocaleString([], {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}) : '—';
  const m = last.match_id && state.matches.find(x => x.match_id === last.match_id);
  const link = m ? ` · <button class="linkbtn" type="button" data-open="${esc(m.match_id)}">${esc(m.my_champion)} v ${esc(m.opp_champion || '?')}</button>` : '';
  $('rec-last').innerHTML = `Last: ${esc(when)} · <span class="${last.status === 'failed' ? 'bad' : ''}">${esc(REC_STATUS[last.status] || last.status)}</span>${link}`;
}

async function refreshRecorder() {
  clearTimeout(recTimer);
  try {
    const r = await api('/api/recorder');
    renderRecorder(r);
    if (!READ_ONLY && (r.armed || (r.last && r.last.status === 'recording'))) recTimer = setTimeout(refreshRecorder, REC_POLL_MS);
  } catch (e) {
    $('rec-line').className = 'rec-line error';
    $('rec-line').textContent = e.status === 404 ? 'This review_app.py has no recorder. Update it.' : `Recorder status unavailable: ${e.message}`;
  }
}

$('rec-armed').onchange = async () => {
  const box = $('rec-armed'), want = box.checked;
  box.disabled = true;
  try {
    renderRecorder(await api('/api/recorder', {armed: want}));
    status(want ? `Auto-record armed ${hhmm()}` : `Auto-record off ${hhmm()}`);
    refreshRecorder();
  } catch (e) {
    box.checked = !want;
    status(`Auto-record not changed: ${e.message}`, 'error');
  } finally { box.disabled = READ_ONLY; }
};
$('rec-last').onclick = e => { const b = e.target.closest('[data-open]'); if (b) openInReview(b.dataset.open); };
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshRecorder(); else clearTimeout(recTimer); });

// ---------------------------------------------------------------- read-only demo
/** Disable every save in the static demo and say so on the control (PLAN.md, "Demo on AWS"). */
function markReadOnly() {
  if (!READ_ONLY) { $('source').textContent = 'local API'; return; }
  for (const id of ['save-review', 'save-focus', 'use-focus', 'edit-focus']) {
    const b = $(id);
    b.disabled = true; b.textContent += ' (demo)'; b.title = 'Read-only demo: nothing is saved.';
  }
  $('source').textContent = DATA_BASE === 'mock/' ? 'mock data · read-only' : 'demo snapshot · read-only';
}

// ---------------------------------------------------------------- views (hash routing)
const VIEWS = ['review', 'charm', 'profile'];

function showView() {
  const name = VIEWS.includes(location.hash.slice(1)) ? location.hash.slice(1) : 'review';
  state.view = name;
  for (const v of VIEWS) $(`view-${v}`).hidden = v !== name;
  document.querySelectorAll('.tabs a').forEach(a => {
    if (a.dataset.view === name) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  document.title = `${name[0].toUpperCase()}${name.slice(1)} · LoLCoach`;
  if (viewLoaders[name]) viewLoaders[name]();
}
window.addEventListener('hashchange', showView);

/** Open a match in the Review view, e.g. from a Charm table row. */
function openInReview(id) {
  if (location.hash !== '#review') location.hash = '#review';
  loadMatch(id);
}

// ---------------------------------------------------------------- keyboard: J/K moments, Space play
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && document.activeElement && document.activeElement.classList.contains('info')) { document.activeElement.blur(); return; }
  if (e.ctrlKey || e.metaKey || e.altKey || state.view !== 'review' || !state.detail || $('detail').hidden) return;
  const t = e.target;
  if (t.closest && t.closest('input, textarea, select, [contenteditable]')) return;
  const key = e.key.toLowerCase();
  if (key === ' ' && t.closest && t.closest('button, a, summary, video, [role="button"]')) return;
  if (key === 'j' || key === 'k') {
    const rows = state.rows || [];
    if (!rows.length) return;
    const i = state.moment ? rows.findIndex(m => m.id === state.moment.id) : -1;
    const next = key === 'k' ? Math.min(rows.length - 1, i + 1) : Math.max(0, i < 0 ? 0 : i - 1);
    selectMoment(rows[next]);
    e.preventDefault();
  } else if (key === ' ' && $('video').src) {
    const v = $('video');
    if (v.paused) v.play().catch(() => {}); else v.pause();
    e.preventDefault();
  }
});

window.addEventListener('beforeunload', e => { if (state.dirty || state.focusDirty) { e.preventDefault(); e.returnValue = ''; } });

// ---------------------------------------------------------------- start
(async () => {
  markReadOnly();
  showView();
  $('matches').innerHTML = skeleton(4);
  try {
    const [matches, focus] = await Promise.all([api('/api/matches'), api('/api/focus')]);
    state.matches = matches;
    showFocus(focus);
    renderMatches();
    const first = filteredMatches()[0] || matches[0];
    if (first) await loadMatch(first.match_id);
    else showReviewEmpty('<strong>No games to review.</strong> Import Ahri/Zoe mid games with <code>python fetch_matches.py --count 20</code>, then reload.');
    if (!first) status('No games imported', 'note');
  } catch (e) {
    $('matches').innerHTML = emptyRow(4, 'Games could not be loaded.');
    showReviewEmpty(`<strong>Could not load your games.</strong> ${esc(e.message)}`, true);
    status(e.message, 'error');
  }
  refreshRecorder();  // after matches, so "last" can name the linked game
})();
