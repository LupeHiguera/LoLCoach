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
  matches: [], detail: null, moment: null, metric: 'gold_diff',
  dirty: false, focusDirty: false, focus: null, videoURL: null, request: 0,
};

// ---------------------------------------------------------------- data
async function api(path, body) {
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
    clearVideo();
    $('review-empty').hidden = true; $('detail').hidden = false;
    renderMatchHeader(); renderMatches(); renderChart(); renderMoments(); renderReviewForm();
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
  const W = 860, H = 250, L = 56, R = 112, T = 14, B = 214;
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

// ---------------------------------------------------------------- moments (auto prompts + saved snapshots)
function momentRows() {
  const d = state.detail, rows = [...d.moments];
  for (const r of d.reviews) {
    if (!rows.some(m => m.id === r.moment_id)) {
      rows.push({id: r.moment_id, start_ms: r.start_ms, end_ms: r.end_ms, kind: 'custom',
        title: r.start_ms === r.end_ms ? `Snapshot at ${clock(r.start_ms)}` : 'Saved moment', description: 'Chosen from the timeline.'});
    }
  }
  return rows.sort((a, b) => a.end_ms - b.end_ms || a.start_ms - b.start_ms);
}

const BASIS = {death: 'event timestamp', deficit: 'sampled snapshot', farm: 'sampled snapshots', custom: 'your pick'};

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
  renderMoments(); renderChart();
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

$('video-file').onchange = () => {
  const file = $('video-file').files[0];
  if (!file) return;
  if (state.videoURL) URL.revokeObjectURL(state.videoURL);
  state.videoURL = URL.createObjectURL(file);
  $('video').src = state.videoURL; $('video').hidden = false;
  $('video-status').textContent = 'Recording loaded. Check that the offset lines up the game clock.';
};
$('video').onerror = () => { if (state.videoURL) $('video-status').textContent = 'The browser cannot play this format. Use an MP4 or WebM recording.'; };
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

// ---------------------------------------------------------------- views (hash routing)
const VIEWS = ['review', 'charm', 'profile'];
const viewLoaders = {};  // view name -> function run on first show

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
})();
