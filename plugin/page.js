// Zeus · Throne (docs/design.md §9): what waits for your yes, what is deploying, what runs now, and what happened.
// A list first: every card and button is in the tab order, and each state is an icon plus a word. Pictures only confirm
// what the words already say (the bolt on an approval, the tablet crumbling on a decline; both skipped with reduced motion).
// Deploy and Roll back are 3-second holds (api.holdConfirm); Decline asks for a reason word. Nothing here decides:
// each button runs zeus through the plugin, and Zeus, with Janus behind him, says yes or no.
leafy.register('throne', (() => {
  const STATE = {pending: ['⏳', 'waiting'], held: ['⏸', 'held'], approved: ['✓', 'approved'], deploying: ['⚡', 'deploying'],
    deployed: ['✓', 'deployed'], 'rolled-back': ['↩', 'rolled back'], failed: ['✕', 'failed'], expired: ['⌛', 'expired'],
    superseded: ['↑', 'superseded'], declined: ['⊘', 'declined'], refused: ['⊘', 'refused'], released: ['⇥', 'released']};
  const CHECKS = {success: ['✓', 'checks passed'], failure: ['✕', 'checks failed'], pending: ['⋯', 'checks running'], none: ['–', 'no checks']};
  const STAGE = {done: ['✓', 'done'], now: ['⚡', 'running'], todo: ['○', 'to come'], failed: ['✕', 'failed here']};
  const REASONS = [['not-now', 'Not now'], ['not-ready', 'Not ready'], ['wrong-commit', 'Wrong commit'], ['testing', 'Still testing']];
  const FILTERS = [['all', 'All'], ['waiting', 'Waiting'], ['expired', 'Expired'], ['failed', 'Failed']];
  const FAILED = ['failed', 'rolled-back', 'refused'];
  let A = null, ROOT = null, S = null, filter = 'all', timer = 0, said = '', heard = '', stale = false;
  const openLogs = new Set();
  const $ = (s, el = ROOT) => el.querySelector(s), $$ = (s, el = ROOT) => [...el.querySelectorAll(s)];
  const e = s => A.esc(String(s ?? ''));
  const name = r => String(r.repo || '').split('/').pop();
  const ver = r => r.version || String(r.sha || '').slice(0, 7);
  const commit = r => `<a href="https://github.com/${e(r.repo)}/commit/${e(r.sha)}" target="_blank" rel="noopener">${e(String(r.sha || '').slice(0, 7))}</a>`;
  const chip = (k, map) => { const [i, w] = map[k] || ['•', k || 'unknown']; return `<span class="chip s-${e(k)}"><span aria-hidden="true">${i}</span> ${e(w)}</span>`; };
  const plural = (n, one) => `${n} ${one}${n === 1 ? '' : 's'}`;
  function ago(iso) {
    const t = Date.parse(iso); if (!t) return '';
    const m = Math.round((Date.now() - t) / 60000);
    if (m < 1) return 'just now'; if (m < 60) return `${m} min ago`; if (m < 24 * 60) return `${Math.round(m / 60)} h ago`;
    return new Date(t).toLocaleDateString([], {weekday: 'short', month: 'short', day: 'numeric'});
  }
  function until(iso) {
    const m = Math.round((Date.parse(iso) - Date.now()) / 60000); if (!(m > 0)) return 'expires now';
    return `expires in ${m < 60 ? `${m} min` : m < 48 * 60 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} d`}`;
  }
  const by = a => a && a.user ? ` by ${e(a.user)}${a.node ? ` · ${e(a.node)}` : ''}${a.via ? ` (${e(a.via)})` : ''}` : '';
  const when = r => (r.deploy && (r.deploy.finished || r.deploy.at)) || (r.approval && r.approval.at) || r.seen || '';

  /* ---------- the four sections ---------- */
  function tablet(r) {
    const refused = r.janus && r.janus.verdict === 'refused' && (r.janus.reasons || []).length;
    const clog = (r.changelog || []).slice(0, 10).map(l => `<li>${e(String(l).replace(/^\s*[-*]\s*/, ''))}</li>`).join('');
    const files = Number.isInteger(r.files) ? plural(r.files, 'file') + ' changed' : 'files: git could not say';
    return `<li class="tablet" data-id="${e(r.id)}">
      <div class="thead"><h4>${e(name(r))} <b>${e(ver(r))}</b></h4>${chip(r.state, STATE)}</div>
      <p class="subj">${e(r.subject)} <span class="meta">· ${commit(r)}</span></p>
      ${clog ? `<ul class="clog" aria-label="What changed">${clog}</ul>` : ''}
      <p class="meta facts">${chip(r.checks || 'none', CHECKS)}<span>${e(files)}</span><span>${e(until(r.expires))}</span></p>
      ${(r.rules_changed || []).length ? `<p class="rules"><span aria-hidden="true">⚑</span> <b>Changes the realm's own rules:</b> ${r.rules_changed.map(f => `<code>${e(f)}</code>`).join(', ')}. Read those files before you hold.</p>` : ''}
      ${r.state === 'held' ? '<p class="note">Janus could not answer last time, so Zeus held it. Hold Deploy again once Janus is back.</p>' : ''}
      ${refused ? `<div class="note bad"><b>Janus refused the last approval:</b><ul>${r.janus.reasons.map(x => `<li>${e(x)}</li>`).join('')}</ul></div>` : ''}
      <div class="acts"><button type="button" class="go">Deploy ${e(name(r))} ${e(ver(r))}</button>
        <button type="button" class="no" aria-expanded="false">Decline</button>
        <button type="button" class="re" data-repo="${e(r.repo)}">Check main again</button></div>
      <form class="why" hidden><fieldset><legend>Why not deploy ${e(name(r))} ${e(ver(r))}?</legend>
        ${REASONS.map(([v, l], i) => `<label><input type="radio" name="why-${e(r.id)}" value="${v}" ${i ? '' : 'checked'}> ${l}</label>`).join('')}</fieldset>
        <div class="acts"><button type="submit">Decline ${e(name(r))} ${e(ver(r))}</button><button type="button" class="keep">Keep it waiting</button></div></form>
    </li>`;
  }
  function running(r) {
    return `<li class="run" data-id="${e(r.id)}">
      <h4>${r.action === 'rollback' ? 'Rolling back' : 'Deploying'} ${e(name(r))} ${e(ver(r))}</h4>
      <ol class="stages" aria-label="Stages">${(r.stages || []).map(s => { const [i, w] = STAGE[s.state] || STAGE.todo;
        return `<li class="st-${e(s.state)}"><span aria-hidden="true">${i}</span> ${e(s.name)} <span class="sr">(${w})</span>${s.name === 'install' ? '<small>from here on, a failure puts the previous version back</small>' : ''}</li>`; }).join('')}</ol>
      <pre class="tail" tabindex="0" aria-label="The last lines of the deploy log">${e((r.tail || []).join('\n'))}</pre>
      <p class="meta">There is no Cancel: half a deploy is worse than a whole one. If it fails after the install, Zeus puts back what ran before.</p>
    </li>`;
  }
  function liveRow(d) {
    const l = d.deployed, p = d.previous, k = d.repo.split('/').pop();
    return `<li class="live" data-repo="${e(d.repo)}">
      <h4>${e(k)}</h4>
      ${l ? `<p><b>${e(ver(l))}</b> <span class="meta">(${commit({...l, repo: d.repo})}) since ${e(ago(l.at))}${by(l.approval)}</span></p>` : '<p class="meta">Nothing deployed by Zeus yet.</p>'}
      <div class="acts">${p ? `<button type="button" class="back" data-sha="${e(p.sha)}">Roll back to ${e(ver(p))}</button>` : ''}
        <button type="button" class="re" data-repo="${e(d.repo)}">Check main again</button></div>
      ${l && !p ? '<p class="meta">Nothing earlier to roll back to.</p>' : ''}
    </li>`;
  }
  function row(r) {
    const re = ['expired', 'superseded'].includes(r.state);
    return `<li class="hrow" data-id="${e(r.id)}">
      <div class="thead"><span>${chip(r.state, STATE)} <b>${e(name(r))} ${e(ver(r))}</b>${r.action === 'rollback' ? ' <span class="meta">(roll back)</span>' : ''}</span><span class="meta">${e(ago(when(r)))}</span></div>
      ${r.reason ? `<p class="meta">${e(r.reason)}</p>` : ''}
      ${r.has_log || re ? `<div class="acts">${r.has_log ? `<button type="button" class="log" aria-expanded="false">Show the log</button>` : ''}${re ? `<button type="button" class="re" data-repo="${e(r.repo)}">Check main again</button>` : ''}</div>` : ''}
      <pre class="tail" tabindex="0" aria-label="Deploy log" hidden></pre>
    </li>`;
  }
  const shown = () => {
    const w = S.waiting, h = S.history;
    return filter === 'waiting' ? w : filter === 'expired' ? h.filter(r => r.state === 'expired') : filter === 'failed' ? h.filter(r => FAILED.includes(r.state)) : w.concat(h);
  };

  function draw() {
    const focus = document.activeElement && ROOT.contains(document.activeElement) ? key(document.activeElement) : null;
    const hist = shown();
    ROOT.innerHTML = `<div class="throne">
      <p class="tline" id="tstatus" role="status">${e(said || S.line)}</p>
      <p class="sr" aria-live="polite" id="tstage"></p>
      ${S.alert ? `<div class="talert ${S.alert.level === 'alert' ? 'bad' : ''}"><p><span aria-hidden="true">⚠</span> <b>Zeus says:</b> ${e(S.alert.message)}</p><button type="button" class="seen">Mark as read</button></div>` : ''}
      <div class="tcols"><div class="tcol">
      <section aria-labelledby="t-wait"><h3 id="t-wait">Waiting for your yes</h3>
        ${S.waiting.length ? `<ul class="tablets">${S.waiting.map(tablet).join('')}</ul>`
          : '<p class="empty">Nothing waits for your yes. A new commit on <code>main</code> shows up here within the hour, or press Check main again under Deployed now.</p>'}</section>
      ${S.deploying.length ? `<section aria-labelledby="t-run"><h3 id="t-run">Deploying</h3><ul class="runs">${S.deploying.map(running).join('')}</ul></section>` : ''}
      </div><div class="tcol">
      <section aria-labelledby="t-live"><h3 id="t-live">Deployed now</h3>
        ${S.deployed.length ? `<ul class="lives">${S.deployed.map(liveRow).join('')}</ul>` : '<p class="empty">Zeus has no repositories yet (repos.json).</p>'}</section>
      <section aria-labelledby="t-hist"><h3 id="t-hist">History</h3>
        <div class="filters" role="group" aria-label="Show">${FILTERS.map(([v, l]) => `<button type="button" data-f="${v}" aria-pressed="${v === filter}">${l}</button>`).join('')}</div>
        ${hist.length ? `<ul class="hist">${hist.map(row).join('')}</ul>` : '<p class="empty">Nothing here yet.</p>'}</section></div></div></div>`;
    wire();
    if (focus) { const el = find(focus); if (el) el.focus(); }
  }
  // a redraw must keep keyboard focus: remember the button by its card and its class, find it again after
  const key = el => { const li = el.closest('[data-id],[data-repo]'), b = el.closest('button,a,pre,input'); return li && b ? [li.dataset.id || li.dataset.repo, b.className.split(' ')[0] || b.tagName] : el.dataset && el.dataset.f ? ['f', el.dataset.f] : null; };
  const find = ([id, cls]) => id === 'f' ? $(`[data-f="${cls}"]`) : $$('[data-id],[data-repo]').filter(li => (li.dataset.id || li.dataset.repo) === id).map(li => li.querySelector(`.${CSS.escape(cls)}`) || li.querySelector(cls.toLowerCase())).find(Boolean);

  /* ---------- what the buttons do: each one asks Zeus and shows what he said ---------- */
  function report(res, done) {
    said = res.ok ? done : res.status === 0 ? "Couldn't reach Elaia's server. Nothing was sent." : (res.data && res.data.msg) || `Zeus said no (${res.status}).`;
    const st = $('#tstatus'); if (st) st.textContent = said;
    A.toast(said.split('\n')[0].slice(0, 200));
    clearTimeout(report.t); report.t = setTimeout(() => { said = ''; const s = ROOT && $('#tstatus'); if (s && S) s.textContent = S.line; }, 30000);
  }
  async function ask(path, body, done, li, effect) {
    const res = await A.post(path, body), ok = res.ok && res.data && res.data.ok;
    report(ok ? res : {ok: false, status: res.status, data: res.data}, done);
    if (ok && li && effect) await effect(li);
    setTimeout(load, 0);           // after the hold has let go of its button, so the redraw isn't held back
    return ok;                     // a hold's onDone: false makes Argus wince instead of nod (Elaia 4.20)
  }
  function wire() {
    $$('.tablet').forEach(li => {
      const r = S.waiting.find(x => x.id === li.dataset.id); if (!r) return;
      A.holdConfirm($('.go', li), () => ask('approve', {id: r.id, sha: r.sha}, `Approved. Zeus is deploying ${name(r)} ${ver(r)}.`, li, x => A.fx.bolt(x)));
      const no = $('.no', li), form = $('.why', li);
      no.onclick = () => { const open = form.hidden; form.hidden = !open; no.setAttribute('aria-expanded', String(open)); if (open) $('input:checked', form).focus(); };
      $('.keep', form).onclick = () => { form.hidden = true; no.setAttribute('aria-expanded', 'false'); no.focus(); };
      form.onsubmit = ev => { ev.preventDefault(); const why = $('input:checked', form).value; form.hidden = true;   // an open form holds redraws back
        ask('decline', {id: r.id, reason: why}, `Declined ${name(r)} ${ver(r)} (${why}). Nothing was deployed.`, li, x => A.fx.pieces(x)); };
    });
    $$('.live').forEach(li => {
      const b = $('.back', li); if (!b) return;
      const d = S.deployed.find(x => x.repo === li.dataset.repo);
      A.holdConfirm(b, () => ask('rollback', {repo: d.repo, sha: b.dataset.sha}, `Rolling ${name(d)} back to ${ver(d.previous)}.`, li, x => A.fx.bolt(x)));
    });
    $$('button.re').forEach(b => b.onclick = () => { b.disabled = true; ask('recheck', {repo: b.dataset.repo}, `Zeus checked main of ${b.dataset.repo.split('/').pop()}.`); });
    $$('button.log').forEach(b => b.onclick = () => toggleLog(b.closest('.hrow'), b));
    $$('[data-f]').forEach(b => b.onclick = () => { filter = b.dataset.f; draw(); });
    const seen = $('.seen'); if (seen) seen.onclick = () => ask('seen', {}, 'Marked as read. Argus calms down on his next look.');
    for (const id of openLogs) { const li = $$('.hrow').find(x => x.dataset.id === id); if (li) toggleLog(li, $('.log', li), true); }
  }
  async function toggleLog(li, b, keep) {
    const pre = $('.tail', li), open = keep || pre.hidden;
    pre.hidden = !open; b.setAttribute('aria-expanded', String(open)); b.textContent = open ? 'Hide the log' : 'Show the log';
    if (!open) { openLogs.delete(li.dataset.id); return; }
    openLogs.add(li.dataset.id);
    const res = await A.get('log?id=' + encodeURIComponent(li.dataset.id));
    pre.textContent = res.ok ? (res.data.lines.join('\n') || 'The log is empty.') : "Couldn't read the log.";
  }

  /* ---------- reading: every 15 s with Elaia's refresh, every 2 s while something deploys ---------- */
  async function load() {
    clearTimeout(timer); timer = 0;
    const res = await A.get('');
    if (!ROOT || !ROOT.isConnected) return;
    if (!res.ok) { ROOT.innerHTML = `<div class="empty"><b>The Throne can't read Zeus right now.</b> Elaia's server didn't answer (${res.status || 'no answer'}). It tries again in 15 seconds.</div>`; return; }
    const busy = $('.hold.holding, .hold.done, form.why:not([hidden])');
    const fresh = JSON.stringify(res.data), changed = !S || fresh !== JSON.stringify(S);
    const was = S ? S.deploying.map(r => r.id) : [];
    S = res.data;
    // a deploy you watched just ended: Argus nods if it is live, winces if it failed or was rolled back (Elaia 4.20, api.argus)
    for (const id of was.filter(id => !S.deploying.some(r => r.id === id))) {
      const st = (S.history.find(r => r.id === id) || {}).state;
      if (st && A.argus) A.argus.react(st === 'deployed' ? 'nod' : ['failed', 'rolled-back'].includes(st) ? 'wince' : 'rest');
    }
    if ((changed || stale) && !busy) { draw(); stale = false; } else if (changed) stale = true;   // never redraw under a hold or an open form
    const stage = S.deploying.map(r => `${name(r)} ${ver(r)}: ${((r.stages || []).find(s => s.state === 'now') || {}).name || 'starting'}`).join('; ');
    if (stage !== heard) { heard = stage; const live = $('#tstage'); if (live && stage) live.textContent = `Deploying ${stage}`; }   // stage changes only, not every line
    if (S.deploying.length) timer = setTimeout(load, 2000);
  }
  return {
    async show(el, api) { A = api; ROOT = el; S = null; said = ''; await load(); },
    refresh(el, api) { A = api; ROOT = el; if (!timer) load(); },
  };
})());
