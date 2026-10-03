'use strict';
/* Watch view: the linked recording, with model coaching revealed at each decision time.
   Uses app.js helpers ($, esc, clock, api, state, status, killStrip, …). API.md: /api/coaching. */

const AFTER_DEATH_MS = 10000;  // a moment stays active this long after its death
const MAX_STEP_MS = 2000;      // larger playhead jumps are seeks, not playback reaching a decision
const ASSESS = {reviewable: 'Reviewable', needs_more_evidence: 'Needs more evidence'};
const CHECKED = 'Checked by you';
const watch = {matchId: null, data: null, error: null, active: null, revealed: new Set(),
  offset: null, playable: false, last: null, request: 0, shown: '', obsOpen: false};
const wv = () => $('watch-video');
const coachMoments = () => (watch.data && watch.data.moments) || [];
/** Coaching can be placed on the video only with a playable file and a known offset. */
const timed = () => watch.playable && Number.isFinite(watch.offset);
const gameMs = () => timed() ? (wv().currentTime - watch.offset) * 1000 : null;

// ---------------------------------------------------------------- load
viewLoaders.watch = () => {
  if (!state.detail) { showWatchState(state.matches.length ? '<strong>No game selected.</strong> Pick a game in Games.' : '<strong>No games to watch.</strong> Press Fetch new in Games, or run <code>python fetch_matches.py --count 20</code>.'); return; }
  if (watch.matchId !== state.detail.match.match_id) loadWatch();
};
document.addEventListener('matchloaded', () => { if (state.view === 'watch') loadWatch(); else watch.matchId = null; });
window.addEventListener('hashchange', () => { if (state.view !== 'watch') wv().pause(); });

function showWatchState(html, error = false) {
  $('watch-main').hidden = true; $('watch-coach').hidden = true; $('watch-missing').hidden = false;
  $('watch-missing-text').className = error ? 'state error' : 'state';
  $('watch-missing-text').innerHTML = html;
}

async function loadWatch() {
  const d = state.detail, m = d.match, request = ++watch.request;
  Object.assign(watch, {matchId: m.match_id, data: null, error: null, active: null, revealed: new Set(), last: null, shown: ''});
  $('watch-missing').hidden = true; $('watch-main').hidden = false; $('watch-coach').hidden = false;
  $('watch-title').textContent = `${m.my_champion} vs ${m.opp_champion || 'unknown opponent'}`;
  $('watch-moments').innerHTML = skeleton(3, 2);
  $('watch-count').textContent = '';
  setupVideo();
  renderScrub(); renderAdvice();
  try {
    const data = await api('/api/coaching?id=' + encodeURIComponent(m.match_id));
    if (request !== watch.request) return;
    watch.data = data;
  } catch (e) {
    if (request !== watch.request) return;
    watch.error = e;
    status(`Coaching: ${e.message}`, 'error');
  }
  renderWatchMoments(); renderScrub(); renderAdvice();
}

// ---------------------------------------------------------------- video
function setupVideo() {
  const v = wv(), rec = state.detail.recording, note = $('watch-video-state');
  v.pause(); v.removeAttribute('src'); v.load(); v.hidden = true;
  watch.playable = false; watch.offset = rec && Number.isFinite(rec.offset_s) ? rec.offset_s : null;
  note.hidden = false; note.className = 'state';
  $('watch-clock').textContent = '';
  if (!rec) {
    note.innerHTML = '<strong>No recording linked to this game.</strong> Coaching is listed below without video. Arm Recorder before your next game, or run <code>python recorder.py --link</code> after Fetch new.';
    return;
  }
  const name = String(rec.path || '').split(/[\\/]/).pop();
  if (!rec.url) {
    note.className = 'state error';
    note.innerHTML = `<strong>The linked recording ${esc(name)} is no longer on disk.</strong> Move it back, or relink it with <code>python recorder.py --link</code>.`;
    return;
  }
  // Large recordings can take several seconds before the first frame; say so instead of a blank box.
  note.innerHTML = `Loading ${esc(name)}…`;
  v.src = rec.url; v.hidden = false;
}

wv().onloadedmetadata = () => {
  watch.playable = true;
  const note = $('watch-video-state');
  note.hidden = watch.offset !== null;
  if (watch.offset === null) note.innerHTML = '<strong>The game clock was not read for this recording,</strong> so coaching is not timed to the video. Pick a moment to read it.';
  renderScrub(); renderAdvice(); tick();
};
wv().onerror = () => {
  if (!wv().getAttribute('src')) return;
  watch.playable = false;
  const note = $('watch-video-state');
  note.hidden = false; note.className = 'state error';
  note.innerHTML = '<strong>The browser cannot play this recording.</strong> Record to MP4, or remux it with <code>ffmpeg -i in.mkv -c copy out.mp4</code>.';
  wv().hidden = true; renderScrub(); renderAdvice();
};
wv().ontimeupdate = () => tick();
wv().onseeked = () => tick();

/** Follow the playhead: clock readout, scrubber, and coaching arriving at its decision time. */
function tick() {
  const now = gameMs();
  if (now === null) return;
  const v = wv(), prev = watch.last;
  watch.last = now;
  $('watch-clock').textContent = `Game ${now < 0 ? '−' : ''}${clock(Math.abs(now))} · video ${clock(v.currentTime * 1000)}`;
  const head = $('watch-playhead');
  if (head) { const x = scrubX(Math.max(0, now)); head.setAttribute('x1', x); head.setAttribute('x2', x); }
  if (prev !== null && !v.seeking && now >= prev && now - prev < MAX_STEP_MS) {
    const arrived = coachMoments().find(m => prev < m.decision_ms && m.decision_ms <= now);
    if (arrived) {
      watch.revealed.add(arrived.bundle);
      setActive(arrived);
      if ($('watch-pause').checked) { v.pause(); status(`Coaching at ${clock(arrived.decision_ms)}. Paused; press Space to continue.`, 'note'); }
      return;
    }
  }
  const here = coachMoments().find(m => m.start_ms <= now && now <= m.death_ms + AFTER_DEATH_MS);
  if (here && here !== watch.active) setActive(here); else renderAdvice();
}

// ---------------------------------------------------------------- moments
function setActive(m) {
  watch.active = m;
  renderWatchMoments(); renderScrub(); renderAdvice();
}

/** Jump to a moment's lead-up; its coaching appears again when playback reaches the decision. */
function selectWatchMoment(m) {
  if (!m) return;
  watch.revealed.delete(m.bundle);
  if (timed()) {
    const target = m.start_ms / 1000 + watch.offset;
    if (target < 0 || target > wv().duration) status(`${clock(m.start_ms)} falls outside this recording. Check the offset.`, 'error');
    else { wv().currentTime = target; watch.last = null; }
  }
  setActive(m);
}

function stepMoment(dir) {
  const list = coachMoments();
  if (!list.length) return;
  const i = watch.active ? list.indexOf(watch.active) : -1;
  selectWatchMoment(list[i < 0 ? (dir > 0 ? 0 : list.length - 1) : Math.min(list.length - 1, Math.max(0, i + dir))]);
}

function resultLabel(m) {
  if (m.review) return ASSESS[m.review.assessment];
  return m.problem && m.problem.startsWith('Bundle failed') ? 'Invalid bundle' : m.observations.length ? 'No review yet' : 'No observations yet';
}

function renderWatchMoments() {
  const list = coachMoments(), body = $('watch-moments');
  if (watch.error) { body.innerHTML = emptyRow(3, 'Coaching could not be loaded.'); $('watch-count').textContent = ''; return; }
  if (!watch.data) return;
  $('watch-count').textContent = `n=${list.length} · ${list.filter(m => m.review).length} reviewed`;
  body.innerHTML = list.map((m, i) => `<tr data-index="${i}" class="${m === watch.active ? 'selected' : ''}">
      <td class="num">${clock(m.decision_ms)}</td>
      <td><button class="linkbtn" type="button">Death at ${clock(m.death_ms)}</button></td>
      <td class="cell-note">${esc(resultLabel(m))}</td></tr>`).join('')
    || emptyRow(3, `No coached moments for this game (${watch.data.scanned} bundles in data/moments).`);
}
$('watch-moments').onclick = e => { const row = e.target.closest('tr[data-index]'); if (row) selectWatchMoment(coachMoments()[Number(row.dataset.index)]); };
$('watch-prev').onclick = () => stepMoment(-1);
$('watch-next').onclick = () => stepMoment(1);

// ---------------------------------------------------------------- scrubber: the whole game, click to seek
const SCRUB = {W: 860, L: 56, R: 72};
let scrubDuration = 1;
const scrubX = t => +(SCRUB.L + (SCRUB.W - SCRUB.L - SCRUB.R) * Math.min(1, t / scrubDuration)).toFixed(1);

function renderScrub() {
  const d = state.detail, box = $('watch-scrub');
  if (!d) { box.innerHTML = ''; return; }
  const {W, L, R} = SCRUB, H = 104, right = W - R;
  scrubDuration = Math.max(d.match.duration_s * 1000, 60000);
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="group" aria-label="Game map: kills, your deaths and coaching points.${timed() ? ' Click to seek.' : ''}">`;
  svg += `<rect class="hit" x="${L}" y="0" width="${right - L}" height="${H - 16}"/>`;
  for (let t = 0; t <= scrubDuration; t += 300000) {
    svg += `<line class="grid" x1="${scrubX(t)}" x2="${scrubX(t)}" y1="4" y2="${H - 22}"/><text class="axis" x="${scrubX(t)}" y="${H - 6}" text-anchor="middle">${clock(t)}</text>`;
  }
  if ((d.kills || []).length) svg += killStrip(d.kills, scrubX, L, right);
  svg += `<text class="axis" x="${L - 6}" y="56" text-anchor="end">Deaths</text>`;
  for (const ts of d.deaths || []) svg += `<text class="death" x="${scrubX(ts)}" y="58" text-anchor="middle">✕<title>Your death at ${clock(ts)}</title></text>`;
  svg += `<text class="axis" x="${L - 6}" y="78" text-anchor="end">Coach</text>`;
  coachMoments().forEach((m, i) => {
    const x0 = scrubX(m.start_ms), x1 = scrubX(m.decision_ms);
    svg += `<rect class="lead${m === watch.active ? ' on' : ''}" x="${x0}" y="68" width="${Math.max(2, x1 - x0)}" height="14"/>`;
    svg += `<path class="coach-mark${m === watch.active ? ' on' : ''}" data-index="${i}" tabindex="0" role="button" d="M${x1} 68l7 7-7 7-7-7z" aria-label="Coaching at ${clock(m.decision_ms)}, death at ${clock(m.death_ms)}: ${esc(resultLabel(m))}"><title>Coaching at ${clock(m.decision_ms)} · ${esc(resultLabel(m))}</title></path>`;
  });
  const n = coachMoments().length;
  svg += `<text class="direct" x="${right + 8}" y="79">${n} coached</text>`;
  if (timed()) svg += `<line class="playhead" id="watch-playhead" x1="${L}" x2="${L}" y1="2" y2="${H - 20}"/>`;
  box.innerHTML = svg + '</svg>';
  if (timed() && watch.last !== null) { const x = scrubX(Math.max(0, watch.last)); $('watch-playhead').setAttribute('x1', x); $('watch-playhead').setAttribute('x2', x); }
  box.querySelectorAll('.coach-mark').forEach(p => {
    const pick = () => selectWatchMoment(coachMoments()[Number(p.dataset.index)]);
    p.onclick = e => { e.stopPropagation(); pick(); };
    p.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(); } };
  });
}

$('watch-scrub').onclick = e => {
  const svg = e.currentTarget.querySelector('svg');
  if (!svg || !timed() || e.target.closest('.coach-mark')) return;
  const r = svg.getBoundingClientRect(), x = (e.clientX - r.left) * SCRUB.W / r.width;
  if (x < SCRUB.L || x > SCRUB.W - SCRUB.R) return;
  const t = (x - SCRUB.L) / (SCRUB.W - SCRUB.L - SCRUB.R) * scrubDuration;
  wv().currentTime = Math.min(wv().duration, Math.max(0, t / 1000 + watch.offset));
  watch.last = null;
};

// ---------------------------------------------------------------- advice
const seekable = ms => ms != null && timed() ? `<button class="linkbtn num" type="button" data-seek="${ms}">${clock(ms)}</button>` : `<span class="num">${ms == null ? '—' : clock(ms)}</span>`;
const evidence = list => `<ul class="evidence">${list.map(x => `<li>${seekable(x.game_ms)} <span class="src">${esc(x.source)}:</span> ${esc(x.text)}</li>`).join('')}</ul>`;

function revealed(m) {
  const now = gameMs();
  return !timed() || watch.revealed.has(m.bundle) || (now !== null && now >= m.decision_ms);
}

function adviceBody(m) {
  if (m.problem && !m.review) {
    let html = `<div class="state${m.problem.startsWith('Bundle failed') ? ' error' : ''}">${esc(m.problem)}</div>`;
    if (m.observations.length) html += observationsBlock(m, true);
    return html;
  }
  const r = m.review;
  let html = `<p class="assess${r.assessment === 'needs_more_evidence' ? ' needs' : ''}">${ASSESS[r.assessment]}</p>`;
  if (r.alternative) {
    html += `<h3>Alternative</h3><p class="advice-action">${esc(r.alternative.action)}</p>`;
    html += `<p><span class="dim">Tradeoff:</span> ${esc(r.alternative.tradeoff)}</p>${evidence(r.alternative.evidence)}`;
  }
  if (r.practice_focus) {
    html += `<h3>Practice focus</h3><p>${esc(r.practice_focus)}</p>`;
    if (!READ_ONLY) html += '<div class="actions"><button class="btn" type="button" id="watch-use-focus">Use as focus</button></div>';
  }
  if (r.claims.length) {
    html += `<h3>Reasoning</h3><ol class="claims">${r.claims.map(c => `<li><span class="dim">${c.kind === 'hypothesis' ? 'Hypothesis' : 'Observation'}:</span> ${esc(c.statement)}${evidence(c.evidence)}</li>`).join('')}</ol>`;
  }
  if (r.missing_evidence.length) html += `<h3>Missing evidence</h3><ul class="plain">${r.missing_evidence.map(x => `<li>${esc(x)}</li>`).join('')}</ul>`;
  return html + observationsBlock(m, false);
}

function observationsBlock(m, open) {
  const unchecked = m.observations.filter(o => o.source !== CHECKED).length;
  const items = m.observations.map(o => `<li>${seekable(o.game_ms)} <span class="src">${esc(o.source)}:</span> ${esc(o.text)}${READ_ONLY ? ''
    : ` <button class="btn" type="button" data-check="${esc(o.id)}" aria-pressed="${o.source === CHECKED}">${o.source === CHECKED ? 'Undo check' : 'Mark checked'}</button>`}</li>`).join('');
  return `<details class="obs"${open || watch.obsOpen ? ' open' : ''}><summary>Model observations (n=${m.observations.length}, ${unchecked} unchecked) · unknowns (n=${m.unknowns.length})</summary>
    ${READ_ONLY || !m.observations.length ? '' : '<p class="dim">Mark one checked only after seeing it in the video at its time.</p>'}
    <ul class="evidence obs-list">${items}</ul>
    ${m.unknowns.length ? `<ul class="plain">${m.unknowns.map(u => `<li><span class="dim">Unknown:</span> ${esc(u)}</li>`).join('')}</ul>` : ''}</details>`;
}

function renderAdvice() {
  const box = $('watch-advice'), m = watch.active;
  let key, html;
  if (watch.error) {
    key = 'error';
    const old = watch.error.status === 404 && watch.error.message !== 'Match not found' && !READ_ONLY;
    html = old ? '<div class="state error"><strong>No coaching endpoint.</strong> This review_app.py has no /api/coaching. Update it and restart it.</div>'
      : READ_ONLY ? '<div class="state">The demo snapshot has no coaching.</div>'
      : `<div class="state error"><strong>Could not load coaching.</strong> ${esc(watch.error.message)} <button class="btn" type="button" id="watch-retry">Retry</button></div>`;
  } else if (!watch.data) {
    key = 'loading'; html = '<div class="state">Loading coaching…</div>';
  } else if (!coachMoments().length) {
    key = 'none';
    html = '<div class="state"><strong>No coaching for this game.</strong> Prepare a moment before one of your deaths with <code>python -m coach.harness prepare</code>, then run <code>observe</code> and <code>review</code> into the same folder under data/moments.</div>';
  } else if (!m) {
    key = 'idle';
    html = `<div class="state">${timed() ? 'Play the recording; coaching appears at each decision time.' : 'Pick a moment to read its coaching.'} J / K steps between moments.</div>`;
  } else {
    const open = revealed(m);
    key = `${m.bundle}:${open}`;
    html = `<p class="advice-head">Death at ${clock(m.death_ms)} · decision ${clock(m.decision_ms)}</p>
      <p class="dim">Evidence ${clock(m.start_ms)}–${clock(m.decision_ms)} · ${m.frames} frames · ${m.state ? 'Riot timeline facts included' : 'no Riot timeline facts'} · sync ${m.sync_verified ? 'checked' : 'unchecked'}</p>`;
    html += open ? adviceBody(m)
      : `<div class="state">Watch the lead-up. Coaching appears at ${clock(m.decision_ms)} (<span id="watch-countdown"></span>). <button class="btn" type="button" id="watch-reveal">Show now</button></div>`;
  }
  if (key !== watch.shown) { box.innerHTML = html; watch.shown = key; }
  const left = $('watch-countdown'), now = gameMs();
  if (left && m && now !== null) left.textContent = now < m.start_ms ? `lead-up starts at ${clock(m.start_ms)}` : `in ${clock(Math.max(0, m.decision_ms - now))}`;
}

$('watch-advice').onclick = e => {
  const t = e.target;
  if (t.id === 'watch-reveal' && watch.active) { watch.revealed.add(watch.active.bundle); renderAdvice(); }
  else if (t.id === 'watch-retry') { watch.matchId = null; loadWatch(); }
  else if (t.id === 'watch-use-focus') useCoachFocus(watch.active);
  else if (t.dataset && t.dataset.check && watch.active) checkObservation(watch.active, t.dataset.check, t.getAttribute('aria-pressed') !== 'true', t);
  else if (t.dataset && t.dataset.seek && timed()) { wv().currentTime = Math.max(0, Number(t.dataset.seek) / 1000 + watch.offset); watch.last = null; }
};

/** Save that you checked (or unchecked) one observation against the video; updates its bundle on disk. */
async function checkObservation(m, id, checked, button) {
  button.disabled = true;
  try {
    const fresh = await api('/api/observation-check', {match_id: watch.matchId, bundle: m.bundle, observation_id: id, checked});
    const list = coachMoments(), i = list.indexOf(m);
    if (i < 0) return;  // the game changed while saving
    list[i] = fresh;
    if (watch.active === m) watch.active = fresh;
    watch.shown = ''; watch.obsOpen = true;
    renderWatchMoments(); renderScrub(); renderAdvice();
    const again = $('watch-advice').querySelector(`[data-check="${CSS.escape(id)}"]`);
    if (again) again.focus();
    status(`${checked ? 'Observation marked checked' : 'Check removed'} ${hhmm()}`);
  } catch (e) {
    status(`Check not saved: ${e.message}`, 'error');
    button.disabled = false;
  }
}

/** Copy the practice focus into the sidebar focus form; nothing is saved until Save focus. */
function useCoachFocus(m) {
  if (!m || !m.review || !m.review.practice_focus) return;
  if (state.focusDirty && !confirm('Replace the unsaved focus draft?')) return;
  const match = state.detail.match;
  openFocusForm();
  $('goal').value = m.review.practice_focus.slice(0, 1000);
  $('why').value = `Model review, ${match.my_champion} vs ${match.opp_champion || '?'}, decision ${clock(m.decision_ms)}: ${m.review.alternative ? m.review.alternative.action : ''}`.slice(0, 1000);
  $('check').value = '';
  state.focusDirty = true;
  $('goal').focus();
}

// ---------------------------------------------------------------- keyboard: J/K coaching, Space play
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey || state.view !== 'watch' || $('watch-main').hidden) return;
  const t = e.target;
  if (t.closest && t.closest('input, textarea, select, [contenteditable]')) return;
  const key = e.key.toLowerCase();
  if (key === ' ' && t.closest && t.closest('button, a, summary, video, [role="button"]')) return;
  if (key === 'j' || key === 'k') { stepMoment(key === 'k' ? 1 : -1); e.preventDefault(); }
  else if (key === ' ' && watch.playable) { const v = wv(); if (v.paused) v.play().catch(() => {}); else v.pause(); e.preventDefault(); }
});

if (state.view === 'watch') showWatchState('Loading games…');
