// Zeus · Throne (docs/design.md §9, §14): the questions sessions asked you, what waits for your yes, what is deploying, what runs now, and what happened.
// A list first: every card and button is in the tab order, and each state is an icon plus a word. Pictures only confirm
// what the words already say (the bolt on an approval, the tablet crumbling on a decline; both skipped with reduced motion).
// Deploy and Roll back are 3-second holds (api.holdConfirm), each with one sentence saying what it does; Decline sits apart
// and asks for a reason word. A request older than what runs (stale, 0.7.0) offers Decline only. Nothing here decides:
// each button runs zeus through the plugin, and Zeus, with Janus behind him, says yes or no.
// Needs your answer (the decision inbox, tier 3): a session's question with its options as buttons. A choice is a tap,
// not a hold, because an answer runs nothing; it is recorded once and can't be changed afterwards.
leafy.register('throne', (() => {
  const STATE = {pending: ['⏳', 'waiting'], held: ['⏸', 'held'], approved: ['✓', 'approved'], deploying: ['⚡', 'deploying'],
    deployed: ['✓', 'deployed'], 'rolled-back': ['↩', 'rolled back'], failed: ['✕', 'failed'], expired: ['⌛', 'expired'],
    superseded: ['↑', 'superseded'], declined: ['⊘', 'declined'], refused: ['⊘', 'refused'], released: ['⇥', 'released'],
    open: ['?', 'needs your answer'], answered: ['✓', 'answered'], old: ['↓', 'old request']};
  const CHECKS = {success: ['✓', 'checks passed'], failure: ['✕', 'checks failed'], pending: ['⋯', 'checks running'], none: ['–', 'no checks']};
  const STAGE = {done: ['✓', 'done'], now: ['⚡', 'running'], todo: ['○', 'to come'], failed: ['✕', 'failed here']};
  const REASONS = [['not-now', 'Not now'], ['not-ready', 'Not ready'], ['wrong-commit', 'Wrong commit'], ['testing', 'Still testing']];
  const FILTERS = [['all', 'All'], ['waiting', 'Waiting'], ['expired', 'Expired'], ['failed', 'Failed']];
  const FAILED = ['failed', 'rolled-back', 'refused'];
  const NEWEST = 5;   // History shows the newest five rows; one control shows the rest
  const LONG = 20;    // notes longer than this get a second Hide the notes at their end
  let A = null, ROOT = null, S = null, filter = 'all', timer = 0, said = '', heard = '', stale = false, askedOpen = false, olderOpen = false;
  const openLogs = new Set(), openNotes = new Set(), openOther = new Set();   // what the reader unfolded stays unfolded across redraws
  const $ = (s, el = ROOT) => el.querySelector(s), $$ = (s, el = ROOT) => [...el.querySelectorAll(s)];
  const e = s => A.esc(String(s ?? ''));
  const name = r => String(r.repo || '').split('/').pop();
  const ver = r => r.version || String(r.sha || '').slice(0, 7);
  const commit = r => `<a class="commit" href="https://github.com/${e(r.repo)}/commit/${e(r.sha)}" target="_blank" rel="noopener">See the change on GitHub (${e(String(r.sha || '').slice(0, 7))})</a>`;
  const liveOf = r => (S.deployed || []).find(d => d.repo === r.repo) || {};
  const runs = d => d && d.running && d.running.version ? String(d.running.version) : '';
  // release notes as readable text: escape first, then only headings, bullets, bold and code spans become plain styled
  // text. A link keeps its words and loses its address (nothing becomes a link); no raw #, * or backtick is left.
  const inline = s => String(s).split(/(`[^`\n]+`)/).map((t, i) => i % 2 ? `<code>${e(t.slice(1, -1))}</code>`
    : e(t).replace(/\[([^\]]*)\]\([^)\s]*\)/g, '$1').replace(/\*\*(.+?)\*\*|__(.+?)__/g, (m, a, b) => `<b>${a || b}</b>`).replace(/[*`]/g, '')).join('');
  const noteLines = r => (r.changelog || []).flatMap(l => String(l).split('\n')).filter(l => l.trim());
  function notes(lines) {
    const out = []; let list = false;
    const close = () => { if (list) { out.push('</ul>'); list = false; } };
    for (const l of lines) {
      const h = /^\s{0,3}#{1,6}\s+(.*?)\s*#*\s*$/.exec(l), li = /^\s*[-*+]\s+(.*)$/.exec(l);
      if (h) { close(); out.push(`<p class="nh">${inline(h[1])}</p>`); }
      else if (li) { if (!list) { out.push('<ul class="nl">'); list = true; } out.push(`<li>${inline(li[1])}</li>`); }
      else { close(); out.push(`<p>${inline(l.replace(/^\s{0,3}>\s?/, ''))}</p>`); }
    }
    close(); return out.join('');
  }
  // the one sentence under Deploy: what happens, and what Zeus puts back if the health check fails. He puts back his
  // own last deploy (state.deployed), which is not always what runs now; the sentence says so when they differ. When the
  // request IS his last deploy (the same commit), restore() in zeus/deploy.py has nothing to put back, and says so.
  function consequence(r) {
    const d = liveOf(r), own = d.deployed && ver(d.deployed), run = runs(d);
    if (own && d.deployed.sha && d.deployed.sha === r.sha)
      return 'This is the version Zeus deployed last. He installs it again, then checks that it works. If the check fails, there is nothing for him to put back.';
    const back = !own ? 'there is no earlier version for him to put back.'
      : run && run !== own && d.drift !== 'same' ? `he puts back ${own}, his own last deploy (not ${run}, which runs now).` : `he puts ${own} back.`;
    return `Zeus installs it, then checks that it works. If the check fails, ${back}`;
  }
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
  const staged = r => r && r.kind === 'staged-pack';   // docs/design.md §15: a pack built on the PC, no "check main again"
  const mb = n => Number.isFinite(n) ? `${(n / 1e6).toFixed(1)} MB` : '? MB';
  const by = a => a && a.user ? ` by ${e(a.user)}${a.node ? ` · ${e(a.node)}` : ''}${a.via ? ` (${e(a.via)})` : ''}` : '';
  const when = r => (r.deploy && (r.deploy.finished || r.deploy.at)) || (r.approval && r.approval.at) || r.seen || '';

  const still = () => !!(window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches);

  /* ---------- needs your answer: the decision inbox ---------- */
  function askCard(r) {
    return `<li class="ask" data-id="${e(r.id)}">
      <div class="thead"><h4>${e(r.title)}</h4>${chip('open', STATE)}</div>
      <p class="meta facts"><span>Asked by <b>${e(r.project)}</b></span><span>${e(ago(r.asked))}</span><span>${e(until(r.expires))}</span></p>
      ${r.context ? `<p class="ctx">${e(r.context)}</p>` : ''}
      <div class="acts opts" role="group" aria-label="Answer: ${e(r.title)}">${(r.options || []).map((o, i) =>
        `<button type="button" class="opt${i + 1} opt" data-i="${i + 1}">${e(o)}</button>`).join('')}</div>
      <p class="meta">One tap answers. Nothing runs because of it, and the answer can't be changed afterwards.</p>
    </li>`;
  }
  function askedRow(r) {
    const a = r.answer, who = a && a.by && a.by.user ? ` by ${e(a.by.user)}${a.by.via ? ` (${e(a.by.via)})` : ''}` : '';
    return `<li class="hrow asked-row" data-id="${e(r.id)}">
      <div class="thead"><span>${chip(r.state, STATE)} <b>${e(r.title)}</b></span><span class="meta">${e(ago((a && a.at) || r.expires))}</span></div>
      <p class="meta">${a ? `Answer: <b>${e(a.option)}</b>${who}` : 'Nobody answered before it expired.'} · asked by ${e(r.project)}</p>
    </li>`;
  }

  /* ---------- the four sections ---------- */
  function tablet(r) {
    // In this order, so a tired reader decides on the first screen: (a) one summary line, (b) Deploy with one sentence of
    // consequence, (c) Decline apart, quieter, with its own sentence, (d) the notes folded, (e) the commit inside the fold.
    // A stale request (older than what runs) says so first and offers Decline only: nothing on it deploys. Stale cards
    // sit under their own small heading, Old requests, after the cards that need a yes.
    const refused = r.janus && r.janus.verdict === 'refused' && (r.janus.reasons || []).length;
    const lines = noteLines(r), open = openNotes.has(r.id), id = e(r.id);
    const files = Number.isInteger(r.files) ? plural(r.files, 'file') + ' changed' : 'files: git could not say';
    const pack = staged(r) && r.pack ? `<p class="meta facts pack"><span>Pack ${e(mb(r.pack.size))} · ${e(plural(Number(r.pack.files) || 0, 'file'))}</span><span>${r.tests ? `Tests: ${e(r.tests)}` : 'No tests reported'} <span class="sr">(as the session reported them)</span></span></p>` : '';
    const decline = `<div class="dont"><button type="button" class="no" aria-expanded="false" aria-describedby="no-${id}">Decline</button>
          <p class="meta" id="no-${id}">Nothing is installed. The request closes.</p></div>`;
    return `<li class="tablet${r.stale ? ' old' : ''}" data-id="${id}">
      ${r.stale ? `<p class="olds"><span aria-hidden="true">↓</span> Older than what runs now (${e(runs(liveOf(r)))})</p>` : ''}
      <div class="thead"><h4>${e(name(r))} <b>${e(ver(r))}</b></h4>${r.stale ? '' : chip(r.state, STATE)}</div>
      ${r.stale ? `<p class="meta">It needs nothing from you and ${e(until(r.expires).replace(/^expires/, 'expires by itself'))}.</p>`
        : `<p class="meta facts">${staged(r) ? '' : chip(r.checks || 'none', CHECKS)}<span>${e(files)}</span><span>${e(until(r.expires))}</span></p>`}
      ${(r.rules_changed || []).length ? `<p class="rules"><span aria-hidden="true">⚑</span> <b>Changes the realm's own rules:</b> ${r.rules_changed.map(f => `<code>${e(f)}</code>`).join(', ')}. Read those files before you hold.</p>` : ''}
      ${r.state === 'held' && !r.stale ? '<p class="note">Janus could not answer last time, so Zeus held it. Hold Deploy again once Janus is back.</p>' : ''}
      ${refused ? `<div class="note bad"><b>Janus refused the last approval:</b><ul>${r.janus.reasons.map(x => `<li>${e(x)}</li>`).join('')}</ul></div>` : ''}
      <div class="decide">${r.stale ? decline
        : `<div class="do"><button type="button" class="go" aria-describedby="go-${id}">Deploy ${e(name(r))} ${e(ver(r))}</button>
          <p class="meta" id="go-${id}">${e(consequence(r))}</p></div>${decline}`}</div>
      <form class="why" hidden><fieldset><legend>Why not deploy ${e(name(r))} ${e(ver(r))}?</legend>
        ${REASONS.map(([v, l], i) => `<label><input type="radio" name="why-${e(r.id)}" value="${v}" ${i ? '' : 'checked'}> ${l}</label>`).join('')}</fieldset>
        <div class="acts"><button type="submit">Decline ${e(name(r))} ${e(ver(r))}</button><button type="button" class="keep">Keep it waiting</button></div></form>
      <button type="button" class="more" aria-expanded="${open}" aria-controls="notes-${id}">${open ? 'Hide the notes' : lines.length ? `Read the notes (${plural(lines.length, 'line')})` : 'More about this version'}</button>
      <div class="fold" id="notes-${id}"${open ? '' : ' hidden'}>
        <p class="subj">${e(r.subject)}</p>
        <p>${commit(r)}</p>${pack}
        ${lines.length ? `<div class="notes" aria-label="What changed">${notes(lines)}</div>` : '<p class="meta">This version came without notes.</p>'}
        ${lines.length > LONG ? '<button type="button" class="less" aria-expanded="true">Hide the notes</button>' : ''}
      </div>
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
  // Deployed now, in words: what runs (when the program answers), and only when Zeus's own record differs, one sentence on
  // how. Roll back keeps its hold and says what it puts in place of what; when the record differs from what runs, its
  // target is older than a person would expect, so it sits folded behind "Other actions". One Check main again per program.
  // While Zeus deploys that program, what runs moves ahead of his record (after the install, before the health check
  // passes), so no drift sentence then: only what is true.
  const DRIFT = {newer: own => `Zeus's own last deploy was ${own}; the newer version was installed some other way.`,
    older: own => `Zeus's own last deploy was ${own}; an older version runs instead.`,
    'no-record': () => 'Zeus has not deployed it yet; this version was installed some other way.'};
  function liveRow(d) {
    const l = d.deployed, p = d.previous, k = e(name(d)), id = e(d.repo.replace('/', '--')), run = runs(d);
    const odd = d.drift in DRIFT, open = openOther.has(d.repo), busy = (S.deploying || []).some(x => x.repo === d.repo);
    const who = l && l.approval && l.approval.user ? `, approved${by(l.approval)}` : '';
    const back = p ? `<div class="act"><button type="button" class="back" data-sha="${e(p.sha)}" aria-describedby="rb-${id}">Roll back to ${e(ver(p))}</button>
        <p class="meta" id="rb-${id}">Zeus puts ${e(ver(p))} back in place of ${e(run || ver(l || {}))}, then checks that it works.</p></div>` : '';
    return `<li class="live" data-repo="${e(d.repo)}">
      <h4>${k}</h4>
      ${run ? `<p class="now">Runs now: <b>${e(run)}</b></p>` : l ? `<p class="now">Last deployed by Zeus: <b>${e(ver(l))}</b></p>` : ''}
      ${busy && odd ? '<p class="meta">Zeus is deploying a new version now.</p>' : odd ? `<p class="meta">${e(DRIFT[d.drift](l ? ver(l) : ''))}</p>`
        : l ? `<p class="meta">Zeus deployed it ${e(ago(l.at))}${who}.</p>` : run ? '' : '<p class="meta">Nothing deployed by Zeus yet.</p>'}
      ${staged(d) ? '<p class="meta">A new version comes from a pack staged by its session.</p>'
        : `<div class="act"><button type="button" class="re" data-repo="${e(d.repo)}" aria-describedby="re-${id}">Check main again</button>
        <p class="meta" id="re-${id}">Looks for a newer version. Installs nothing.</p></div>`}
      ${back && odd ? `<button type="button" class="other" aria-expanded="${open}" aria-controls="other-${id}">${open ? 'Hide other actions' : 'Other actions'}</button>
        <div class="fold" id="other-${id}"${open ? '' : ' hidden'}>${back}</div>` : back}
      ${l && !p ? '<p class="meta">Nothing earlier to roll back to.</p>' : ''}
    </li>`;
  }
  function row(r) {
    // one control per row (its log); "Check main again" lives once per program under Deployed now
    return `<li class="hrow" data-id="${e(r.id)}">
      <div class="thead"><span>${chip(r.stale ? 'old' : r.state, STATE)} <b>${e(name(r))} ${e(ver(r))}</b>${r.action === 'rollback' ? ' <span class="meta">(roll back)</span>' : ''}</span><span class="meta">${e(ago(when(r)))}</span></div>
      ${r.reason ? `<p class="meta">${e(r.reason)}</p>` : ''}
      ${r.has_log ? '<div class="acts"><button type="button" class="log" aria-expanded="false">Show the log</button></div>' : ''}
      <pre class="tail" tabindex="0" aria-label="Deploy log" hidden></pre>
    </li>`;
  }
  const shown = () => {
    const w = S.waiting, h = S.history;
    return filter === 'waiting' ? w : filter === 'expired' ? h.filter(r => r.state === 'expired') : filter === 'failed' ? h.filter(r => FAILED.includes(r.state)) : h;   // All = what happened; waiting requests have their own section
  };

  function draw() {
    const focus = document.activeElement && ROOT.contains(document.activeElement) ? key(document.activeElement) : null;
    const hist = shown(), fresh = S.waiting.filter(r => !r.stale), olds = S.waiting.filter(r => r.stale);
    ROOT.innerHTML = `<div class="throne">
      <p class="tline" id="tstatus" role="status">${e(said || S.line)}</p>
      <p class="sr" aria-live="polite" id="tstage"></p>
      ${S.alert ? `<div class="talert ${S.alert.level === 'alert' ? 'bad' : ''}"><p><span aria-hidden="true">⚠</span> <b>Zeus says:</b> ${e(S.alert.message)}</p><button type="button" class="seen">Mark as read</button></div>` : ''}
      <div class="tcols"><div class="tcol">
      <section aria-labelledby="t-ask"><h3 id="t-ask">Needs your answer</h3>
        ${(S.asks || []).length ? `<ul class="asks">${S.asks.map(askCard).join('')}</ul>` : '<p class="empty">Nothing is waiting for you.</p>'}
        ${(S.asked || []).length ? `<details class="asked"${askedOpen ? ' open' : ''}><summary>Answered and expired (${S.asked.length})</summary><ul class="hist">${S.asked.map(askedRow).join('')}</ul></details>` : ''}</section>
      <section aria-labelledby="t-wait"><h3 id="t-wait">Waiting for your yes</h3>
        ${fresh.length ? `<ul class="tablets">${fresh.map(tablet).join('')}</ul>`
          : '<p class="empty">Nothing waits for your yes. A new version shows up here within the hour.</p>'}
        ${olds.length ? `<h4 class="oldh" id="t-old">Old requests</h4><ul class="tablets" aria-labelledby="t-old">${olds.map(tablet).join('')}</ul>` : ''}</section>
      ${S.deploying.length ? `<section aria-labelledby="t-run"><h3 id="t-run">Deploying</h3><ul class="runs">${S.deploying.map(running).join('')}</ul></section>` : ''}
      </div><div class="tcol">
      <section aria-labelledby="t-live"><h3 id="t-live">Deployed now</h3>
        ${S.deployed.length ? `<ul class="lives">${S.deployed.map(liveRow).join('')}</ul>` : '<p class="empty">Zeus has no repositories yet (repos.json).</p>'}</section>
      <section aria-labelledby="t-hist"><h3 id="t-hist">History</h3>
        <div class="filters" role="group" aria-label="Show">${FILTERS.map(([v, l]) => `<button type="button" data-f="${v}" aria-pressed="${v === filter}">${l}</button>`).join('')}</div>
        ${hist.length ? `<ul class="hist">${hist.slice(0, olderOpen ? hist.length : NEWEST).map(row).join('')}</ul>` : '<p class="empty">Nothing here yet.</p>'}
        ${hist.length > NEWEST ? `<button type="button" class="older" id="t-older" aria-expanded="${olderOpen}">${olderOpen ? `Show only the newest ${NEWEST}` : `Show older (${hist.length - NEWEST})`}</button>` : ''}</section></div></div></div>`;
    wire();
    if (focus) { const el = find(focus); if (el) el.focus(); }
  }
  // a redraw must keep keyboard focus: remember the button by its card and its class, find it again after
  const key = el => { const li = el.closest('[data-id],[data-repo]'), b = el.closest('button,a,pre,input'); return li && li !== ROOT && ROOT.contains(li) && b ? [li.dataset.id || li.dataset.repo, b.className.split(' ')[0] || b.tagName] : el.dataset && el.dataset.f ? ['f', el.dataset.f] : el.id ? ['#', el.id] : null; };
  const find = ([id, cls]) => id === 'f' ? $(`[data-f="${cls}"]`) : id === '#' ? $(`#${CSS.escape(cls)}`) : $$('[data-id],[data-repo]').filter(li => (li.dataset.id || li.dataset.repo) === id).map(li => li.querySelector(`.${CSS.escape(cls)}`) || li.querySelector(cls.toLowerCase())).find(Boolean);

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
    $$('.ask').forEach(li => {
      const r = (S.asks || []).find(x => x.id === li.dataset.id); if (!r) return;
      $$('button.opt', li).forEach(b => b.onclick = () => {
        const i = Number(b.dataset.i), opt = r.options[i - 1];
        $$('button.opt', li).forEach(x => { x.disabled = true; });
        b.setAttribute('aria-pressed', 'true');
        ask('answer', {id: r.id, index: i}, `Answered "${r.title}": ${opt}.`, li, x => { if (!still() && A.fx && A.fx.glint) A.fx.glint(x); })
          .then(ok => { if (!ok) $$('button.opt', li).forEach(x => { x.disabled = false; x.removeAttribute('aria-pressed'); }); });
      });
    });
    const det = $('details.asked'); if (det) det.ontoggle = () => { askedOpen = det.open; };
    $$('.tablet').forEach(li => {
      const r = S.waiting.find(x => x.id === li.dataset.id); if (!r) return;
      const go = $('.go', li);   // a stale request has no Deploy: nothing on its card can send approve
      if (go && !r.stale) A.holdConfirm(go, () => ask('approve', {id: r.id, sha: r.sha}, `Approved. Zeus is deploying ${name(r)} ${ver(r)}.`, li, x => A.fx.bolt(x)));
      const more = $('.more', li), fold = $('.fold', li);
      more.onclick = () => { const open = fold.hidden; fold.hidden = !open; more.setAttribute('aria-expanded', String(open));
        const n = noteLines(r).length; more.textContent = open ? 'Hide the notes' : n ? `Read the notes (${plural(n, 'line')})` : 'More about this version';
        if (open) openNotes.add(r.id); else openNotes.delete(r.id); };
      const less = $('.less', li); if (less) less.onclick = () => { more.click(); more.focus(); };   // closes from the end, focus back on top
      const no = $('.no', li), form = $('.why', li);
      no.onclick = () => { const open = form.hidden; form.hidden = !open; no.setAttribute('aria-expanded', String(open)); if (open) $('input:checked', form).focus(); };
      $('.keep', form).onclick = () => { form.hidden = true; no.setAttribute('aria-expanded', 'false'); no.focus(); };
      form.onsubmit = ev => { ev.preventDefault(); const why = $('input:checked', form).value; form.hidden = true;   // an open form holds redraws back
        ask('decline', {id: r.id, reason: why}, `Declined ${name(r)} ${ver(r)} (${why}). Nothing was deployed.`, li, x => A.fx.pieces(x)); };
    });
    $$('.live').forEach(li => {
      const d = S.deployed.find(x => x.repo === li.dataset.repo), other = $('.other', li);
      if (other) other.onclick = () => { const box = $('.fold', li), open = box.hidden; box.hidden = !open; other.setAttribute('aria-expanded', String(open));
        other.textContent = open ? 'Hide other actions' : 'Other actions'; if (open) openOther.add(d.repo); else openOther.delete(d.repo); };
      const b = $('.back', li); if (!b) return;
      A.holdConfirm(b, () => ask('rollback', {repo: d.repo, sha: b.dataset.sha}, `Rolling ${name(d)} back to ${ver(d.previous)}.`, li, x => A.fx.bolt(x)));
    });
    $$('button.re').forEach(b => b.onclick = () => { b.disabled = true; ask('recheck', {repo: b.dataset.repo}, `Zeus checked main of ${b.dataset.repo.split('/').pop()}.`); });
    $$('button.log').forEach(b => b.onclick = () => toggleLog(b.closest('.hrow'), b));
    $$('[data-f]').forEach(b => b.onclick = () => { filter = b.dataset.f; draw(); });
    const older = $('#t-older'); if (older) older.onclick = () => { olderOpen = !olderOpen; draw(); };
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
    const before = Object.fromEntries((S ? S.deploying : []).map(r => [r.id, (r.stages || []).map(s => s.state)]));   // each stage's state at the last look
    S = res.data;
    // a deploy you watched just ended: Argus nods if it is live, winces if it failed or was rolled back (Elaia 4.20, api.argus)
    for (const id of was.filter(id => !S.deploying.some(r => r.id === id))) {
      const st = (S.history.find(r => r.id === id) || {}).state;
      if (st && A.argus) A.argus.react(st === 'deployed' ? 'nod' : ['failed', 'rolled-back'].includes(st) ? 'wince' : 'rest');
    }
    if ((changed || stale) && !busy) { draw(); stale = false; } else if (changed) stale = true;   // never redraw under a hold or an open form
    // a deploy you are watching moves on: the stage that just finished glints (Elaia 4.18, api.fx) and Argus glances at the one
    // running now for a moment (Elaia 4.20, api.argus.watch). Only for stages seen before, so opening the page mid-deploy stays still
    if (!busy) S.deploying.filter(r => r.id in before).forEach(r => (r.stages || []).forEach((s, k) => {
      const li = $(`.run[data-id="${CSS.escape(r.id)}"] .stages li:nth-child(${k + 1})`); if (!li || before[r.id][k] === s.state) return;
      if (s.state === 'done' && A.fx) A.fx.glint(li);
      if (s.state === 'now' && A.argus) { A.argus.watch(li); setTimeout(() => A.argus.watch(null), 1500); }
    }));
    const stage = S.deploying.map(r => `${name(r)} ${ver(r)}: ${((r.stages || []).find(s => s.state === 'now') || {}).name || 'starting'}`).join('; ');
    if (stage !== heard) { heard = stage; const live = $('#tstage'); if (live && stage) live.textContent = `Deploying ${stage}`; }   // stage changes only, not every line
    if (S.deploying.length) timer = setTimeout(load, 2000);
  }
  return {
    async show(el, api) { A = api; ROOT = el; S = null; said = ''; await load(); },
    refresh(el, api) { A = api; ROOT = el; if (!timer) load(); },
  };
})());
