const $ = id => document.getElementById(id);
const state = {matches: [], detail: null, moment: null, metric: 'gold_diff', dirty: false, focusDirty: false, videoURL: null, request: 0};
const queues = {420: 'Solo/duo', 440: 'Flex', 400: 'Normal draft'};
const time = ms => `${Math.floor(ms / 60000)}:${String(Math.floor(ms / 1000) % 60).padStart(2, '0')}`;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
const signed = n => n == null ? 'unavailable' : `${n > 0 ? '+' : ''}${n}`;
let noticeTimer;
function notice(message, error = false) { $('notice').textContent = message; $('notice').classList.toggle('error', error); $('notice').hidden = false; clearTimeout(noticeTimer); noticeTimer = setTimeout(() => $('notice').hidden = true, error ? 10000 : 4000); }
async function api(path, body) { const r = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); const data = await r.json(); if (!r.ok) throw Error(data.error || 'Request failed'); return data; }
function discardReview() { return !state.dirty || confirm('Leave this unsaved review?'); }
function filteredMatches() { return state.matches.filter(m => (!$('champion').value || m.my_champion === $('champion').value) && (!$('queue').value || String(m.queue_id) === $('queue').value) && (!$('result').value || String(m.win) === $('result').value)); }
function renderMatches() {
  const matches = filteredMatches(); $('match-count').textContent = matches.length;
  $('matches').innerHTML = matches.map(m => `<button class="match ${state.detail?.match.match_id === m.match_id ? 'active' : ''}" data-id="${esc(m.match_id)}"><span class="match-top"><span>${esc(m.my_champion)} <span class="muted">vs ${esc(m.opp_champion || '?')}</span></span><span class="${m.win ? 'win' : 'loss'}">${m.win ? 'W' : 'L'}</span></span><small>${new Date(m.game_start_ms).toLocaleDateString()} · ${time(m.duration_s * 1000)} · ${esc(queues[m.queue_id] || m.queue_id)} · ${esc(m.patch)}</small></button>`).join('') || '<p class="muted small">No imported games match these filters.</p>';
  $('matches').querySelectorAll('button').forEach(b => b.onclick = () => loadMatch(b.dataset.id));
}
function clearVideo() { if (state.videoURL) URL.revokeObjectURL(state.videoURL); state.videoURL = null; $('video').pause(); $('video').removeAttribute('src'); $('video').load(); $('video').hidden = true; $('video-file').value = ''; $('video-offset').value = '0'; $('video-status').textContent = ''; }
async function loadMatch(id) {
  if (!discardReview()) return;
  const request = ++state.request;
  try {
    const detail = await api('/api/match?id=' + encodeURIComponent(id));
    if (request !== state.request) return;
    state.detail = detail; state.moment = null; state.dirty = false; clearVideo();
    $('detail').hidden = false; $('empty').hidden = true; $('review-form').hidden = true; $('review-title').textContent = 'Choose a moment above.'; $('review-description').textContent = ''; $('saved').textContent = '';
    const m = detail.match; $('match-title').textContent = `${m.my_champion} vs ${m.opp_champion || 'unknown opponent'}`;
    $('match-meta').textContent = `${new Date(m.game_start_ms).toLocaleDateString()} · ${queues[m.queue_id] || m.queue_id} · PATCH ${m.patch} · ${time(m.duration_s * 1000)}`;
    $('match-result').textContent = m.win ? 'Victory' : 'Defeat'; $('match-result').className = `badge ${m.win ? 'win' : 'loss'}`;
    renderMatches(); renderChart(); renderMoments(); renderReviews();
  } catch(e) { notice(e.message, true); }
}
function renderChart() {
  const d = state.detail, frames = d.frames, metric = state.metric;
  const points = frames.filter(f => Number.isFinite(f[metric]));
  if (!points.length) { $('chart').innerHTML = '<p class="muted">No comparable snapshots available for this metric.</p>'; return; }
  const width = 900, height = 255, left = 65, top = 20, bottom = 215;
  const duration = Math.max(d.match.duration_s*1000, ...frames.map(f => f.time), 60000);
  const limit = Math.max(metric === 'cs_diff' ? 10 : 100, ...points.map(f => Math.abs(f[metric]))) * 1.12;
  const x = t => left + (width-left-20)*t/duration, y = v => top + (bottom-top)*(1-v/limit)/2;
  let path = '', prev = null;
  for (const p of frames) {
    if (!Number.isFinite(p[metric])) { prev = null; continue; }
    path += `${!prev || p.time-prev.time > d.cadence_ms*1.25 ? 'M' : 'L'}${x(p.time)},${y(p[metric])} `; prev = p;
  }
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="group" aria-label="${esc(metric)} difference timeline. Focus a snapshot for its value; press Enter to review.">`;
  for (const v of [-limit, 0, limit]) svg += `<line x1="${left}" x2="880" y1="${y(v)}" y2="${y(v)}" stroke="${v === 0 ? '#66735a' : '#343e2e'}" stroke-dasharray="4 5"/><text x="55" y="${y(v)+4}" text-anchor="end" fill="#a5af9e" font-size="12">${signed(Math.round(v))}</text>`;
  for (let t = 0; t <= duration; t += 300000) svg += `<text x="${x(t)}" y="244" text-anchor="middle" fill="#a5af9e" font-size="12">${time(t)}</text>`;
  if (state.moment) svg += `<rect x="${x(state.moment.start_ms)}" y="${top}" width="${Math.max(3,x(state.moment.end_ms)-x(state.moment.start_ms))}" height="${bottom-top}" fill="#d4ed96" opacity=".08"/>`;
  svg += `<path d="${path}" fill="none" stroke="#d4ed96" stroke-width="2.5"/>`;
  for (const ts of d.deaths) svg += `<text x="${x(ts)}" y="${bottom+6}" text-anchor="middle" fill="#e79c8d" font-size="19"><title>Your death at ${time(ts)}</title>×</text>`;
  points.forEach((f, i) => { svg += `<circle class="chart-point" cx="${x(f.time)}" cy="${y(f[metric])}" r="4" tabindex="0" role="button" data-index="${i}" aria-label="Review ${time(f.time)}, difference ${signed(f[metric])}"><title>${time(f.time)}: ${signed(f[metric])}</title></circle>`; });
  $('chart').innerHTML = svg + '</svg>';
  $('chart-readout').textContent = state.moment ? `Selected: ${time(state.moment.start_ms)}–${time(state.moment.end_ms)}` : 'Select a snapshot to bookmark it.';
  $('chart').querySelectorAll('.chart-point').forEach(p => {
    const f = points[Number(p.dataset.index)];
    const readout = () => $('chart-readout').textContent = `${time(f.time)} · Gold ${signed(f.gold_diff)} · XP ${signed(f.xp_diff)} · Lane CS ${signed(f.cs_diff)}`;
    const select = () => selectMoment({id:`custom-${f.time}`, start_ms:f.time, end_ms:f.time, title:`Snapshot at ${time(f.time)}`, description:'What was happening here? Use a recording or mark the cause as unconfirmed.', kind:'custom'});
    p.onmouseenter = readout; p.onfocus = readout; p.onclick = select; p.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(); } };
  });
}
function renderMoments() {
  const moments = state.detail.moments; $('moment-count').textContent = moments.length;
  $('moments').innerHTML = moments.map((m,i) => `<button class="moment ${state.moment?.id === m.id ? 'active' : ''}" data-index="${i}"><span class="time">${time(m.start_ms)}–${time(m.end_ms)}</span><strong>${esc(m.title)}</strong><small>${m.kind === 'death' ? 'EVENT TIMESTAMP' : 'SAMPLED OBSERVATION'}${state.detail.reviews.some(r => r.moment_id === m.id) ? ' · SAVED' : ''}</small></button>`).join('') || '<p class="muted small">No automatic prompts in this game. Select any timeline snapshot to review your own moment.</p>';
  $('moments').querySelectorAll('button').forEach(b => b.onclick = () => selectMoment(moments[Number(b.dataset.index)]));
}
function selectMoment(moment, savedReview) {
  if (!discardReview()) return;
  state.moment = moment; state.dirty = false;
  const review = savedReview || state.detail.reviews.find(r => r.moment_id === moment.id);
  $('review-form').reset(); $('review-form').hidden = false; $('review-title').textContent = `${time(moment.start_ms)} · ${moment.title}`; $('review-description').textContent = moment.description;
  for (const key of ['reason', 'evidence', 'observation', 'alternative']) if (review) $(key).value = review[key];
  $('saved').textContent = review ? 'Saved bookmark loaded' : '';
  renderMoments(); renderChart();
}
function renderReviews() {
  const reviews = state.detail.reviews; $('review-count').textContent = reviews.length;
  $('reviews').innerHTML = reviews.map((r,i) => `<button class="bookmark" data-index="${i}"><strong>${time(r.start_ms)}–${time(r.end_ms)}</strong> <small>· ${esc(r.reason)} · ${esc({stats_only:'timeline only',recording:'recording reviewed',memory:'recollection'}[r.evidence])}</small><p>${esc(r.observation)}</p>${r.alternative ? `<p class="muted">Next time: ${esc(r.alternative)}</p>` : ''}</button>`).join('') || '<p class="muted small">Your observations and next steps will live here.</p>';
  $('reviews').querySelectorAll('button').forEach(b => { b.onclick = () => { const r = reviews[Number(b.dataset.index)]; const m = state.detail.moments.find(m => m.id === r.moment_id) || {id:r.moment_id,start_ms:r.start_ms,end_ms:r.end_ms,title:'Saved moment',description:'Revisit your observation and the evidence behind it.'}; selectMoment(m,r); }; });
}
function showFocus(f) { $('focus-title').textContent = f.goal || 'Choose one thing you can control.'; $('focus-why').textContent = f.why_text || 'Start with a reviewed moment below, then turn it into a small practice goal.'; $('focus-check').textContent = f.check_text ? `After playing: ${f.check_text}` : ''; $('goal').value = f.goal; $('why').value = f.why_text; $('check').value = f.check_text; $('edit-focus').textContent = f.goal ? 'Edit focus ↗' : 'Set a focus ↗'; }
$('edit-focus').onclick = () => { $('focus-form').hidden = false; $('goal').focus(); };
$('cancel-focus').onclick = async () => { if (state.focusDirty && !confirm('Discard the unsaved focus changes?')) return; try { showFocus(await api('/api/focus')); state.focusDirty=false; $('focus-form').hidden=true; } catch(e) { notice(e.message,true); } };
$('focus-form').oninput = () => state.focusDirty = true;
$('focus-form').onsubmit = async e => { e.preventDefault(); const b = e.submitter; b.disabled = true; try { showFocus(await api('/api/focus',{goal:$('goal').value,why_text:$('why').value,check_text:$('check').value})); state.focusDirty=false; $('focus-form').hidden=true; notice('Focus saved. Keep it small and observable.'); } catch(e) { notice(e.message,true); } finally { b.disabled=false; } };
$('review-form').oninput = () => { state.dirty=true; $('saved').textContent='Unsaved changes'; };
$('review-form').onsubmit = async e => {
  e.preventDefault(); if (!state.moment) return; const b=e.submitter; b.disabled=true;
  const id=state.detail.match.match_id, moment=state.moment;
  const body={match_id:id,moment_id:moment.id,start_ms:moment.start_ms,end_ms:moment.end_ms};
  for (const k of ['reason','evidence','observation','alternative']) body[k]=$(k).value;
  try { await api('/api/reviews',body); if (state.detail.match.match_id===id && state.moment?.id===moment.id) { state.dirty=false; $('saved').textContent='Saved locally'; } const fresh=await api('/api/match?id='+encodeURIComponent(id)); if (state.detail.match.match_id===id) { state.detail.reviews=fresh.reviews; renderReviews(); renderMoments(); } notice('Review saved.'); } catch(e) { notice(e.message,true); } finally { b.disabled=false; }
};
$('use-focus').onclick = () => { if (state.focusDirty && !confirm('Replace the unsaved focus draft?')) return; $('focus-form').hidden=false; $('goal').value=$('alternative').value.slice(0,1000); $('why').value=`${state.detail.match.my_champion} vs ${state.detail.match.opp_champion}, ${time(state.moment.start_ms)}: ${$('observation').value}`.slice(0,1000); $('check').value=''; state.focusDirty=true; $('goal').focus(); $('focus-form').scrollIntoView({behavior:'smooth',block:'center'}); };
for (const id of ['champion','queue','result']) $(id).onchange = () => { renderMatches(); /* Keep an open review visible when filters change. */ };
document.querySelectorAll('[data-metric]').forEach(b => b.onclick = () => { state.metric=b.dataset.metric; document.querySelectorAll('[data-metric]').forEach(t=>t.classList.toggle('active',t===b)); if (state.detail) renderChart(); });
$('video-file').onchange = () => { const f=$('video-file').files[0]; if (!f) return; if (state.videoURL) URL.revokeObjectURL(state.videoURL); state.videoURL=URL.createObjectURL(f); $('video').src=state.videoURL; $('video').hidden=false; $('video-status').textContent='Recording selected. Check the clock alignment before reviewing.'; };
$('video').onerror = () => $('video-status').textContent='This video format could not be played by the browser. Try an MP4 or WebM recording.';
$('seek-video').onclick = () => { const v=$('video'), offset=Number($('video-offset').value); if (!state.moment || !Number.isFinite(v.duration) || !Number.isFinite(offset)) { notice('Choose a moment and a playable recording first.',true); return; } const target=state.moment.start_ms/1000+offset; if(target<0 || target>v.duration) { notice('This moment falls outside the recording. Check your clock offset.',true); return; } v.currentTime=target; $('video-status').textContent=`Video positioned at ${time(target*1000)}. Game review starts at ${time(state.moment.start_ms)}.`; };
window.addEventListener('beforeunload', e => { if(state.dirty || state.focusDirty) { e.preventDefault(); e.returnValue=''; } });
(async () => { try { const [matches,focus]=await Promise.all([api('/api/matches'),api('/api/focus')]); state.matches=matches; showFocus(focus); renderMatches(); const first=filteredMatches()[0]; if(first) await loadMatch(first.match_id); } catch(e) { notice(e.message,true); $('empty').querySelector('p').textContent='Could not load your games. Check that the local review server is running, then refresh.'; } })();
