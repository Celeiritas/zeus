// Zeus · Throne in a real browser, under Elaia, against a fake zeus (tests/throne/fake_zeus.py). tests/throne/run.sh copies
// this file into an argus-ui checkout's tests/ and runs it there, so './lib' is argus-ui's test helper.
// zeus#11 "done means": every field on a request card; a 1 s press sends nothing; a 3 s press approves {id, sha} with who
// is asking; the keyboard hold works; 375 px without sideways scroll; reduced motion draws no ring; every state is an
// icon and a word; an expired row has Check main again. Plus: decline with a reason word, Home's "Needs you", the alert.
// 0.5.0, the decision inbox (docs/design.md §14): Needs your answer above Waiting for your yes, its empty state, an
// option is a tap (pointer, Enter or Space) that sends {id, index}, what a session wrote stays text, Home counts asks,
// api/throne/summary, 375 px and reduced motion.
// 0.7.0, readable on a phone (docs/design.md §9): what really runs (a health address the test serves itself), a request
// older than that is stale (no Deploy, never approved), Deploy with its sentence above the fold at 412 x 915 under 150
// lines of notes, the notes folded and shown without markdown marks, Decline 16 px away, one Check main again per program,
// Roll back folded when the record differs, History 5 rows then Show older, 44 px controls, sizes 16/14/12 and weights
// 400/600, no sideways scroll at 360 px.
const {BASE, check, page, center, noOverflow, shot, run} = require('./lib');
const fs = require('fs'), path = require('path'), http = require('http');
const D = process.env.ZEUS_DATA, SHA = 'a'.repeat(40), PREV = 'b'.repeat(40);
const WHO = {'Tailscale-User-Login': 'owner@example.com', 'X-Forwarded-For': '203.0.113.7'};
const iso = h => new Date(Date.now() + h * 3600e3).toISOString();
function rec(id, extra = {}, folder = 'queue') {
  const r = {id, action: 'deploy', state: 'pending', repo: 'you/argus-ui', sha: SHA, previous: PREV, subject: 'Mentor: Learn phase', version: '4.9.0',
    checks: 'success', changelog: ['- Learn cards before practice', '- A hint ladder'], files: 12, rules_changed: [], seen: iso(-1), expires: iso(48), approval: null, deploy: null, ...extra};
  fs.writeFileSync(path.join(D, folder, id + '.json'), JSON.stringify(r));
}
function askRec(id, extra = {}) {
  const r = {id, kind: 'decision', tier: 3, state: 'open', project: 'zeus', title: 'Tiers as proposed?', options: ['Yes, as proposed', 'Tier 1 without the second tap'],
    context: 'Tier 1 is a deploy that changes the realm\'s own rules.', asked: iso(-2), expires: iso(70), by: {user: 'owner', via: 'ssh'}, answer: null, zeus: '0.5.0', sig: '0'.repeat(64), ...extra};
  fs.writeFileSync(path.join(D, 'queue', id + '.json'), JSON.stringify(r));
}
const calls = () => { try { return fs.readFileSync(path.join(D, 'calls.jsonl'), 'utf8').trim().split('\n').filter(Boolean).map(l => JSON.parse(l)); } catch (e) { return []; } };
run(async b => {
  rec('20261023-090000-argus-ui-aaaaaaa');
  rec('20261023-080000-realm-ccccccc', {repo: 'you/realm', sha: 'c'.repeat(40), version: null, subject: 'Widen the inbox route', checks: 'none', files: 2, rules_changed: ['janus/routes.json']});
  rec('20261020-090000-hermes-ddddddd', {repo: 'you/hermes', sha: 'd'.repeat(40), version: '0.9.0', expires: iso(-2)});
  rec('20261019-090000-argus-ui-eeeeeee', {state: 'failed', version: '4.8.1', reason: 'the build failed before the install; nothing was changed'}, 'queue/.done');
  fs.writeFileSync(path.join(D, 'log', 'deploy-20261019-090000-argus-ui-eeeeeee.log'), 'zeus: t deploying you/argus-ui 4.8.1\nbuild: error\n');
  fs.writeFileSync(path.join(D, 'state', 'you--argus-ui.json'), JSON.stringify({repo: 'you/argus-ui',
    deployed: {sha: 'f'.repeat(40), version: '4.8.0', at: iso(-30), approval: {user: 'owner@example.com', via: 'elaia', node: 'phone'}}, previous: {sha: PREV, version: '4.7.0'}}));
  fs.writeFileSync(process.env.ZEUS_ALERT, JSON.stringify({job: 'zeus-deploy', level: 'warn', message: 'argus-ui 4.8.1 failed before the install; nothing was changed'}));

  const p = await page(b); await p.setExtraHTTPHeaders(WHO);
  await p.goto(BASE + 'home'); await p.waitForTimeout(800);
  check(/Review 2 deploys/.test(await p.locator('.home').textContent()), 'Home: Needs you says the Throne has deploys to review');
  await p.goto(BASE + 'throne'); await p.locator('.throne').waitFor();
  // Elaia names the page (Throne before her 4.34 shell, Approvals since); nothing answers argus-ui's health here, so the line
  // says Zeus's own last deploy, "deployed", never "live" (0.7.0)
  check(['Throne', 'Approvals'].includes(await p.locator('.title h1').textContent()) && /2 deploys waiting · argus-ui 4.8.0 deployed/.test(await p.locator('#tstatus').textContent()), 'a side page titled by Elaia; the status line says what waits and what Zeus deployed');
  const t = p.locator('.tablet[data-id="20261023-090000-argus-ui-aaaaaaa"]'), txt = await t.textContent();
  for (const [what, re] of [['repository and version', /argus-ui\s*4\.9\.0/], ['subject', /Mentor: Learn phase/], ['commit', /aaaaaaa/], ['changelog', /A hint ladder/],
    ['checks', /checks passed/], ['files', /12 files changed/], ['expiry', /expires in (47 h|2 d)/]]) check(re.test(txt), `a request card shows the ${what}`);
  check(await t.locator(`a[href="https://github.com/you/argus-ui/commit/${SHA}"]`).count() === 1, 'the commit links to GitHub');
  check(/Nothing is waiting for you\./.test(await p.locator('section[aria-labelledby="t-ask"]').textContent()), 'Needs your answer: the empty state says nothing is waiting for you');
  check(await p.locator('#t-ask').evaluate(h => !!(h.compareDocumentPosition(document.getElementById('t-wait')) & Node.DOCUMENT_POSITION_FOLLOWING)), 'Needs your answer comes above Waiting for your yes');
  const realm = p.locator('.tablet[data-id="20261023-080000-realm-ccccccc"]');
  check(/Changes the realm's own rules: janus\/routes\.json/.test(await realm.locator('.rules').textContent()) && /no checks/.test(await realm.textContent()), "a config change wears the rules badge, naming the files");
  check(/Zeus says: argus-ui 4\.8\.1 failed/.test(await p.locator('.talert').textContent()), "Zeus's alert shows at the top");

  const col = s => p.locator(s).evaluate(e => e.closest('.tcol').getBoundingClientRect().x);
  check(await col('#t-wait') < await col('#t-live') && await col('#t-ask') === await col('#t-wait') && Math.abs((await p.locator('#t-ask').boundingBox()).y - (await p.locator('#t-live').boundingBox()).y) < 2,
    'wide screen: Needs your answer, then Waiting for your yes, on the left; Deployed now beside them on the right');
  await p.evaluate(() => { window.moved = []; const a = Element.prototype.animate; Element.prototype.animate = function (k, o) { if (this.id === 'argus') moved.push(o && o.duration); return a.call(this, k, o); }; });
  const go = t.locator('button.go'), [x, y] = await center(go);
  check(await go.locator('.hold-label').textContent() === 'Deploy argus-ui 4.9.0' && /hold 3 seconds/.test(await go.getAttribute('aria-label')), 'the button is verb-first, names what it deploys, and says it must be held');
  check((await go.boundingBox()).height >= 44, '44 px hit area');
  await p.mouse.move(x, y); await p.mouse.down(); await p.waitForTimeout(1000); await p.mouse.up(); await p.waitForTimeout(400);
  check(!calls().some(c => c[0] === 'approve'), 'a 1 s press sends nothing');
  await p.mouse.down(); await p.waitForTimeout(3300); await p.mouse.up();
  await p.locator('.run', {hasText: 'Deploying argus-ui 4.9.0'}).waitFor({timeout: 5000});
  const ap = calls().find(c => c[0] === 'approve');
  check(JSON.stringify(ap) === JSON.stringify(['approve', '20261023-090000-argus-ui-aaaaaaa', '--sha', SHA, '--user', 'owner@example.com', '--ip', '203.0.113.7']), 'a 3 s hold approves that id and commit, passing who is asking to Zeus');
  const reacts = await p.evaluate(() => !!document.getElementById('argus') && typeof look === 'object' && typeof look.react === 'function');
  check(!reacts || await p.evaluate(() => moved.length > 0), reacts ? 'Argus nods when the approval went through (Elaia 4.20)' : 'Argus reactions: this Elaia is older than 4.20, skipped');
  check(/Approved\. Zeus is deploying argus-ui 4\.9\.0/.test(await p.locator('#tstatus').textContent()), 'the status line says what happened');
  const stages = await p.locator('.run .stages li').allTextContents();
  check(stages.length === 5 && /fetch.*\(done\)/.test(stages[0]) && /build.*\(running\)/.test(stages[2]) && /install.*\(to come\)/.test(stages[3]), 'the five stages show from the live log, each with a word');
  check(/building the package/.test(await p.locator('.run .tail').textContent()) && await p.locator('.run button', {hasText: /cancel/i}).count() === 0, 'the log tail shows, and there is no Cancel');
  // the deploy moves on (the log gains the install mark): the build stage glints, install is running now (Elaia 4.18+, api.fx)
  const hasFx = await p.evaluate(() => typeof fx === 'object' && typeof fx.glint === 'function');
  await p.evaluate(() => { window.glints = []; if (typeof fx === 'object') { const g = fx.glint; fx.glint = (el, o) => { glints.push(el.textContent.trim()); return g(el, o); }; } });
  fs.appendFileSync(path.join(D, 'log', 'deploy-20261023-090000-argus-ui-aaaaaaa.log'), 'ZEUS_STAGE=install\ninstalling\n');
  await p.locator('.run .stages li.st-now', {hasText: 'install'}).waitFor({timeout: 5000});
  const glinted = await p.waitForFunction(() => window.glints.length > 0, null, {timeout: 3000}).then(() => true, () => false);
  check(!hasFx || (glinted && await p.evaluate(() => glints.length === 1 && /build/.test(glints[0]))), hasFx ? 'the stage that just finished (build) glints, once' : 'glints: this Elaia is older than 4.18, skipped');
  check(await p.locator('.tablet').count() === 1, 'the approved request left the waiting list');
  await shot(p, 'throne');

  // decline: a reason word, then the tablet goes
  await realm.locator('button.no').click();
  check(await realm.locator('button.no').getAttribute('aria-expanded') === 'true' && await p.evaluate(() => document.activeElement.name.startsWith('why-')), 'Decline opens the reasons, focus on the first');
  await realm.locator('input[value="not-ready"]').check(); await realm.locator('button[type=submit]').click();
  await p.locator('.tablet').first().waitFor({state: 'detached', timeout: 5000}).catch(() => {});
  check(JSON.stringify(calls().find(c => c[0] === 'decline')) === JSON.stringify(['decline', '20261023-080000-realm-ccccccc', '--reason', 'not-ready', '--user', 'owner@example.com', '--ip', '203.0.113.7']), 'Decline sends the reason word');
  check(await p.locator('.tablet').count() === 0 && /Nothing waits for your yes/.test(await p.locator('.throne').textContent()), 'the list is empty and says what happens next');

  // deployed now: roll back by holding Space
  const live = p.locator('.live[data-repo="you/argus-ui"]');
  check(/4\.8\.0/.test(await live.textContent()) && /by owner@example\.com · phone \(elaia\)/.test(await live.textContent()), 'Deployed now: version, when, who and from where');
  const back = live.locator('button.back');
  check(await back.locator('.hold-label').textContent() === 'Roll back to 4.7.0', 'Roll back names its target');
  await back.focus(); await p.keyboard.down(' '); await p.waitForTimeout(3300); await p.keyboard.up(' '); await p.waitForTimeout(600);
  check(JSON.stringify(calls().find(c => c[0] === 'rollback')) === JSON.stringify(['rollback', 'you/argus-ui', '--sha', PREV, '--user', 'owner@example.com', '--ip', '203.0.113.7']), 'holding Space for 3 s rolls back to the target the page showed');

  // history: filters, states as icon + word, expired rows can ask again, logs open
  const hist = p.locator('.hist');
  check(await hist.locator('.hrow', {hasText: 'hermes 0.9.0'}).count() === 1 && await hist.locator('button.re').count() === 0
    && await p.locator('.live[data-repo="you/hermes"] button.re').count() === 1, 'an expired request is in History; Check main again is under Deployed now, not on the row');
  const chips = await p.locator('.throne .chip').evaluateAll(cs => cs.map(c => [c.querySelector('[aria-hidden]')?.textContent.trim(), c.textContent.replace(/^\S+\s*/, '').trim()]));
  check(chips.length >= 3 && chips.every(([i, w]) => i && w.length > 2), `every state is an icon and a word (${chips.map(c => c.join(' ')).join(', ')})`);
  await p.locator('[data-f="failed"]').click();
  check(await p.locator('[data-f="failed"]').getAttribute('aria-pressed') === 'true' && await hist.locator('.hrow').count() === 1, 'the Failed filter shows only the failed deploy');
  await hist.locator('button.log').click();
  await hist.locator('.tail', {hasText: 'build: error'}).waitFor({timeout: 3000});
  check(await hist.locator('button.log').getAttribute('aria-expanded') === 'true', "Show the log opens that deploy's log");
  await p.locator('.talert .seen').click(); await p.waitForTimeout(800);
  check(calls().some(c => c[0] === 'seen') && await p.locator('.talert').count() === 0, 'Mark as read clears the alert');
  check(p.errors.length === 0, 'no page errors ' + p.errors.join(' | '));

  // without tailscale serve's headers Zeus is asked anyway, and his refusal is shown
  rec('20261023-100000-argus-ui-aaaaaab', {sha: SHA, version: '4.9.1'});
  const q = await page(b); await q.goto(BASE + 'throne'); await q.locator('.tablet').waitFor();
  const qg = q.locator('.tablet button.go'), [qx, qy] = await center(qg);
  await q.mouse.move(qx, qy); await q.mouse.down(); await q.waitForTimeout(3300); await q.mouse.up();
  await q.locator('#tstatus', {hasText: 'no identity'}).waitFor({timeout: 5000});
  check(await q.locator('.tablet').count() === 1, 'no identity: Zeus refuses, the page says why, and the request still waits');

  // phone and reduced motion
  const ph = await page(b, 375, 760); await ph.emulateMedia({reducedMotion: 'reduce'}); await ph.goto(BASE + 'throne'); await ph.locator('.tablet').waitFor();
  check(await noOverflow(ph), '375 px: no sideways scrolling');
  check(Math.abs(await ph.locator('#t-wait').evaluate(e => e.closest('.tcol').getBoundingClientRect().x) - await ph.locator('#t-live').evaluate(e => e.closest('.tcol').getBoundingClientRect().x)) < 2, '375 px: one column');
  await shot(ph, 'throne-phone');
  check(await ph.locator('.tablet button.go .hold-ring').evaluate(e => getComputedStyle(e).display) === 'none', 'reduced motion: no ring on the hold');
  check((await ph.locator('.tablet button.go').boundingBox()).height >= 44, 'phone: 44 px hit area');

  // ---- the decision inbox (0.5.0) ----
  const HOSTILE = '<img src=x onerror="window.pwned=1">"</script><b>Ship it?</b>';
  askRec('ask-20261109-080000-zeus');
  askRec('ask-20261109-070000-spartamelee', {project: 'spartamelee', title: HOSTILE, options: ['<b>yes</b>', 'no & "later"', 'x'.repeat(80)],
    context: 'line one\n<script>window.pwned=2</script> & more', asked: iso(-5)});
  askRec('ask-20261101-070000-hub', {project: 'hub', title: 'Old question', asked: iso(-200), expires: iso(-100)});
  askRec('ask-20261108-070000-mnemosyne', {project: 'mnemosyne', title: 'Back up the drives nightly?', state: 'answered', options: ['Nightly', 'Weekly'],
    answer: {option: 'Weekly', index: 2, at: iso(-3), by: {user: 'owner@example.com', via: 'elaia'}}});
  const k = await page(b); await k.setExtraHTTPHeaders(WHO);
  await k.goto(BASE + 'home'); await k.waitForTimeout(800);
  check(/Answer 2, review 1/.test(await k.locator('.home').textContent()), "Home's Needs you counts open asks next to deploys");
  const sum = await k.evaluate(async u => (await fetch(u)).json(), BASE + 'api/throne/summary');
  check(sum.asks === 2 && sum.deploys === 1 && sum.needs_you === 3 && /2 questions for you/.test(sum.line), 'api/throne/summary counts asks and deploys ' + JSON.stringify(sum));
  await k.goto(BASE + 'throne'); await k.locator('.ask').first().waitFor();
  check(await k.locator('.ask').count() === 2, 'Needs your answer lists the open asks; the expired and answered ones are not buttons');
  check(await k.locator('.ask').first().getAttribute('data-id') === 'ask-20261109-070000-spartamelee', 'the ask waiting longest is on top');
  const ev = k.locator('.ask[data-id="ask-20261109-070000-spartamelee"]');
  check(await k.evaluate(() => window.pwned === undefined) && await ev.locator('img, script, h4 b').count() === 0, "what a session wrote stays text: no element is made from it");
  check(await ev.locator('h4').textContent() === HOSTILE && await ev.locator('button.opt1').textContent() === '<b>yes</b>' && await ev.locator('button.opt2').textContent() === 'no & "later"',
    'the title and options read exactly as asked');
  check(/line one\n<script>/.test(await ev.locator('.ctx').textContent()), 'the context keeps its line breaks, as text');
  const askTxt = await k.locator('.ask[data-id="ask-20261109-080000-zeus"]').textContent();
  check(/Asked by zeus/.test(askTxt) && /expires in (69|70) h|expires in 3 d/.test(askTxt) && /needs your answer/.test(askTxt), 'a card says who asked, when it expires, and its state in words');
  check(await k.locator('.ask button.go, .ask .hold').count() === 0, 'an answer is a tap: no hold button on an ask');
  await k.locator('details.asked summary').click();
  check(/Answer: Weekly by owner@example\.com \(elaia\)/.test(await k.locator('details.asked').textContent()) && /Nobody answered before it expired/.test(await k.locator('details.asked').textContent()),
    'Answered and expired: the answer, who gave it, and the one nobody answered');
  await shot(k, 'throne-asks');
  // keyboard: Tab reaches the first option, Enter answers it
  await k.evaluate(() => { window.glints = []; if (typeof fx === 'object') { const g = fx.glint; fx.glint = (el, o) => { glints.push(el.className); return g(el, o); }; } });
  await k.locator('#tstatus').evaluate(el => { el.tabIndex = -1; el.focus(); });
  let reached = false;
  for (let i = 0; i < 40 && !reached; i++) { await k.keyboard.press('Tab'); reached = await k.evaluate(() => document.activeElement.matches('.ask[data-id="ask-20261109-070000-spartamelee"] button.opt1')); }
  check(reached, 'keyboard: Tab reaches the first option of the first ask');
  const visible = await k.evaluate(() => getComputedStyle(document.activeElement).outlineStyle !== 'none' || getComputedStyle(document.activeElement).boxShadow !== 'none');
  check(visible, 'keyboard: the focused option shows its focus');
  await k.keyboard.press('Enter');
  await k.locator('.ask[data-id="ask-20261109-070000-spartamelee"]').waitFor({state: 'detached', timeout: 5000});
  check(JSON.stringify(calls().find(c => c[0] === 'answer')) === JSON.stringify(['answer', 'ask-20261109-070000-spartamelee', '--index', '1', '--user', 'owner@example.com', '--ip', '203.0.113.7']),
    'Enter on an option answers that ask with its number, passing who is answering');
  check(/Answered/.test(await k.locator('#tstatus').textContent()), 'the status line says it was answered');
  // Space on an option of the other ask
  await k.locator('.ask[data-id="ask-20261109-080000-zeus"] button.opt2').focus(); await k.keyboard.press(' ');
  await k.locator('.ask').first().waitFor({state: 'detached', timeout: 5000});
  check(calls().filter(c => c[0] === 'answer').length === 2 && /Nothing is waiting for you/.test(await k.locator('section[aria-labelledby="t-ask"]').textContent()), 'Space answers too; then the empty state returns');
  const hasGlint = await k.evaluate(() => typeof fx === 'object' && typeof fx.glint === 'function');
  check(!hasGlint || await k.evaluate(() => glints.length >= 1), hasGlint ? 'an answer glints the card (motion allowed)' : 'glint: this Elaia is older than 4.18, skipped');
  check(k.errors.length === 0, 'no page errors (asks) ' + k.errors.join(' | '));

  // a second answer to an answered ask is refused by Zeus and the page says so
  const r2 = await k.evaluate(async u => { const r = await fetch(u, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({id: 'ask-20261109-080000-zeus', index: 1})}); return r.json(); },
    BASE + 'api/throne/answer');
  check(r2.ok === false && /already answered/.test(r2.msg), 'a second answer is refused');

  // phone, reduced motion: one column, no sideways scroll with an 80-character option, no glint
  askRec('ask-20261109-090000-zeus', {title: 'A long one ' + 'w'.repeat(120), options: ['y'.repeat(80), 'no']});
  const pr = await page(b, 375, 760); await pr.setExtraHTTPHeaders(WHO); await pr.emulateMedia({reducedMotion: 'reduce'});
  await pr.goto(BASE + 'throne'); await pr.locator('.ask').waitFor();
  check(await noOverflow(pr), '375 px: an ask with long words does not scroll sideways');
  check((await pr.locator('.ask button.opt2').boundingBox()).height >= 44, '375 px: options have a 44 px hit area');
  await shot(pr, 'throne-asks-phone');
  await pr.evaluate(() => { window.glints = []; if (typeof fx === 'object') { const g = fx.glint; fx.glint = (el, o) => { glints.push(el.className); return g(el, o); }; } });
  await pr.locator('.ask button.opt2').click();
  await pr.locator('.ask').waitFor({state: 'detached', timeout: 5000});
  check(await pr.evaluate(() => glints.length === 0), 'reduced motion: answering moves nothing');

  // ---- 0.7.0: readable on a phone ----
  for (const f of ['queue', 'queue/.done', 'log']) for (const n of fs.readdirSync(path.join(D, f))) if (/\.(json|log)$/.test(n)) fs.unlinkSync(path.join(D, f, n));
  const srv = http.createServer((q, s) => { const m = /^\/v\/([\w.+-]+)$/.exec(q.url); s.writeHead(m ? 200 : 404, {'Content-Type': 'application/json'}); s.end(m ? JSON.stringify({ok: true, version: m[1]}) : '{}'); });
  await new Promise(r => srv.listen(0, '127.0.0.1', r));
  const at = v => `http://127.0.0.1:${srv.address().port}/v/${v}`;
  fs.writeFileSync(process.env.ZEUS_CONFIG, JSON.stringify({version: 1, repos: {'you/argus-ui': {health: {http: at('4.36.0')}}, 'you/hermes': {health: {http: at('0.9.0')}}}}));
  fs.writeFileSync(path.join(D, 'state', 'you--argus-ui.json'), JSON.stringify({repo: 'you/argus-ui',
    deployed: {sha: 'f'.repeat(40), version: '4.33.0', at: iso(-30), approval: {user: 'owner@example.com', via: 'elaia', node: 'phone'}}, previous: {sha: PREV, version: '4.32.0'}}));
  fs.writeFileSync(path.join(D, 'state', 'you--hermes.json'), JSON.stringify({repo: 'you/hermes',
    deployed: {sha: 'd'.repeat(40), version: '0.9.0', at: iso(-50), approval: {user: 'owner@example.com', via: 'elaia', node: 'phone'}}, previous: {sha: 'e'.repeat(40), version: '0.8.0'}}));
  const NOTES = [];
  for (let i = 0; i < 25; i++) NOTES.push(`## Part ${i + 1}`, `- **Bold ${i}** with \`code_${i}\` and *soft* words`, `- a [link ${i}](https://example.com/${i}) stays words`, `Plain line ${i} about \`~/.config/x-${i}.json\`.`, '', `### Small heading ${i}`);
  const FRESH = '20261111-050000-argus-ui-1111111', OLD = '20261101-050000-argus-ui-3333333';
  rec(FRESH, {sha: '1'.repeat(40), version: '4.37.0', subject: 'Merge pull request #41 from you/feat/x', changelog: NOTES.slice(0, 150)});
  rec(OLD, {sha: '3'.repeat(40), version: '4.33.0', subject: 'Merge pull request #40 from you/feat/y', changelog: ['## 4.33.0', '- **Pads is now Controllers.**'], expires: iso(30)});
  const STATES = ['deployed', 'declined', 'superseded', 'expired', 'failed', 'deployed', 'rolled-back', 'declined', 'deployed', 'superseded', 'deployed', 'expired'];
  STATES.forEach((st, i) => { const id = `202610${String(10 + i).padStart(2, '0')}-090000-argus-ui-${String(i).padStart(7, '0')}`;
    rec(id, {state: st, version: `4.${20 + i}.0`, reason: st === 'deployed' ? undefined : `it was ${st}`}, 'queue/.done');
    fs.writeFileSync(path.join(D, 'log', `deploy-${id}.log`), `zeus: t deploying you/argus-ui 4.${20 + i}.0\n`); });
  const before = calls().length;

  const m = await page(b, 412, 915); await m.setExtraHTTPHeaders(WHO);
  await m.goto(BASE + 'throne'); await m.locator(`.tablet[data-id="${FRESH}"]`).waitFor();
  const line = await m.locator('#tstatus').textContent();
  check(/1 deploy waiting · 1 old request, expires \w+ · argus-ui 4\.36\.0 running · hermes 0\.9\.0 running/.test(line) && !/live/.test(line), 'the line says what runs, and the old request in a few words: ' + line);
  const sum2 = await m.evaluate(async u => (await fetch(u)).json(), BASE + 'api/throne/summary');
  check(sum2.deploys === 1 && sum2.needs_you === 1 && sum2.stale === 1, 'summary: the old request needs nobody ' + JSON.stringify(sum2));
  const card = m.locator(`.tablet[data-id="${FRESH}"]`), go2 = card.locator('button.go');
  const fold = await m.evaluate(() => { const t = document.getElementById('tabbar'); const r = t && t.getBoundingClientRect(); return r && r.height && getComputedStyle(t).position === 'fixed' && r.top < innerHeight ? r.top : innerHeight; });
  const gb = await go2.boundingBox();
  check(await m.evaluate(() => scrollY) === 0 && gb.y + gb.height <= fold, `412 x 915: the Deploy button is on the first screen (bottom ${Math.round(gb.y + gb.height)} px, fold ${Math.round(fold)} px)`);
  check(/Zeus installs it, then checks that it works\. If the check fails, he puts back 4\.33\.0, his own last deploy \(not 4\.36\.0, which runs now\)\./.test(await card.locator('.do .meta').textContent())
    && (await go2.getAttribute('aria-describedby')) === `go-${FRESH}`, 'one sentence under Deploy says what happens and what he puts back, tied to the button');
  const nb = await card.locator('button.no').boundingBox();
  check(nb.y - (gb.y + gb.height) >= 16 || nb.x - (gb.x + gb.width) >= 16, `Decline is apart from Deploy (${Math.round(nb.y - gb.y - gb.height)} px below)`);
  check(/Nothing is installed\. The request closes\./.test(await card.locator('.dont').textContent()), 'Decline says what it does');
  const more = card.locator('button.more');
  check(await more.textContent() === 'Read the notes (125 lines)' && await more.getAttribute('aria-expanded') === 'false' && await card.locator('.fold').isHidden(), 'the notes are folded behind one control that counts their lines');
  await shot(m, 'throne-412-waiting');
  if (process.env.SHOTS) await card.screenshot({path: path.join(process.env.SHOTS, 'card-412-waiting.png')});
  await more.click();
  const shownNotes = await card.locator('.notes').innerText();
  check(await more.getAttribute('aria-expanded') === 'true' && await card.locator('.fold').isVisible() && !/##|\*\*|`|[#*]/.test(shownNotes) && /Part 25/.test(shownNotes) && /code_3/.test(shownNotes),
    'opened, the notes read as text: no ##, ** or backtick left');
  check(await card.locator('.notes a').count() === 0 && /link 4 stays words/.test(shownNotes) && await card.locator('.notes b').count() === 25 && await card.locator('.notes code').count() >= 50, 'bold and code are styled text; a link keeps its words and is no link');
  check((await card.locator('a.commit').boundingBox()).height >= 44, 'the commit link inside the fold is a 44 px target');
  if (process.env.SHOTS) await card.screenshot({path: path.join(process.env.SHOTS, 'card-412-notes-open.png')});
  await more.click();

  const old = m.locator(`.tablet[data-id="${OLD}"]`);
  check(/^\s*↓ Older than what runs now \(4\.36\.0\)/.test(await old.innerText()) && await old.locator('button.go, .hold').count() === 0 && await old.locator('button.no').count() === 1,
    'a stale card says first that it is older than what runs, and offers Decline only');
  check(/To go back to an older version, use Roll back below/.test(await old.textContent()), 'a stale card points to Roll back');
  await old.scrollIntoViewIfNeeded();
  if (process.env.SHOTS) await old.screenshot({path: path.join(process.env.SHOTS, 'card-412-stale.png')});
  for (const bt of await old.locator('button:visible').all()) if (!/Decline/.test(await bt.textContent())) await bt.click();
  await old.locator('button.more').focus(); await m.keyboard.down(' '); await m.waitForTimeout(3300); await m.keyboard.up(' '); await m.waitForTimeout(500);
  check(!calls().slice(before).some(c => c[0] === 'approve'), 'nothing on a stale card sends approve');

  const lv = m.locator('.live[data-repo="you/argus-ui"]'), hv = m.locator('.live[data-repo="you/hermes"]');
  check(/Runs now: 4\.36\.0/.test(await lv.textContent()) && /Zeus's own last deploy was 4\.33\.0; the newer version was installed by hand\./.test(await lv.textContent()), 'Deployed now: what runs, and one sentence because the record differs');
  check(await lv.locator('button.other').getAttribute('aria-expanded') === 'false' && await lv.locator('button.back').isHidden(), 'drift: Roll back sits folded behind Other actions');
  check(/Runs now: 0\.9\.0/.test(await hv.textContent()) && !/own last deploy/.test(await hv.textContent()) && await hv.locator('button.back').isVisible() && await hv.locator('button.other').count() === 0, 'no drift: no extra sentence, Roll back in view');
  check(/Zeus puts 0\.8\.0 back in place of 0\.9\.0, then checks that it works\./.test(await hv.textContent()), 'Roll back names its true target and what it replaces');
  check(await m.locator('button.re').count() === 2 && await lv.locator('button.re').count() === 1 && await hv.locator('button.re').count() === 1, 'one Check main again per program, under Deployed now only');
  check(/Looks for a newer version\. Installs nothing\./.test(await lv.textContent()), 'Check main again says it installs nothing');
  await hv.scrollIntoViewIfNeeded(); if (process.env.SHOTS) await hv.screenshot({path: path.join(process.env.SHOTS, 'live-412-same.png')});
  await lv.locator('button.other').click();
  check(await lv.locator('button.other').getAttribute('aria-expanded') === 'true' && await lv.locator('button.back').isVisible()
    && /Zeus puts 4\.32\.0 back in place of 4\.36\.0/.test(await lv.textContent()), 'Other actions opens Roll back, which says it replaces what runs now');
  await lv.scrollIntoViewIfNeeded(); if (process.env.SHOTS) await lv.screenshot({path: path.join(process.env.SHOTS, 'live-412-drift.png')});

  const hrows = m.locator('.hist .hrow'), older = m.locator('#t-older');
  const total = STATES.length;   // All is what happened: the two waiting requests are not repeated there (0.7.0)
  check(await hrows.count() === 5 && await older.textContent() === `Show older (${total - 5})` && await older.getAttribute('aria-expanded') === 'false'
    && await m.locator('.hist .hrow', {hasText: '4.37.0'}).count() === 0, 'History shows the newest 5 rows of what happened (not the waiting ones again), then Show older');
  const fr = await m.locator('[data-f]').evaluateAll(bs => bs.map(x => Math.round(x.getBoundingClientRect().top)));
  await m.locator('section[aria-labelledby="t-hist"]').scrollIntoViewIfNeeded();
  if (process.env.SHOTS) await m.locator('section[aria-labelledby="t-hist"]').screenshot({path: path.join(process.env.SHOTS, 'history-412-folded.png')});
  await older.click();
  check(await hrows.count() === total && await m.locator('#t-older').getAttribute('aria-expanded') === 'true' && await m.locator('.hist button.re').count() === 0, 'Show older shows the rest; no row carries Check main again');
  await m.locator('[data-f="waiting"]').click();
  check(/old request/.test(await m.locator('.hist .hrow', {hasText: '4.33.0'}).locator('.chip').textContent()) && /waiting/.test(await m.locator('.hist .hrow', {hasText: '4.37.0'}).locator('.chip').textContent()),
    'the Waiting filter marks the stale request "old request" (icon and word)');
  await m.locator('[data-f="all"]').click();
  if (process.env.SHOTS) await m.locator('section[aria-labelledby="t-hist"]').screenshot({path: path.join(process.env.SHOTS, 'history-412-open.png')});
  await card.locator('button.more').click();
  const small = await m.locator('.throne').evaluate(root => [...root.querySelectorAll('button, a, summary, input, select, [tabindex="0"]')]
    .filter(x => x.offsetParent !== null && !x.closest('[hidden]')).map(x => { const r = x.getBoundingClientRect(); return [x.textContent.trim().slice(0, 30) || x.tagName, Math.round(r.width), Math.round(r.height)]; })
    .filter(([, w, h]) => w < 44 || h < 44));
  check(small.length === 0, 'every visible control is at least 44 x 44 px ' + JSON.stringify(small));
  const type = await m.locator('.throne').evaluate(root => { const bad = new Set();
    for (const x of root.querySelectorAll('*')) { if (x.offsetParent === null || ![...x.childNodes].some(n => n.nodeType === 3 && n.textContent.trim())) continue;
      const c = getComputedStyle(x); if (!['12px', '14px', '16px'].includes(c.fontSize) || !['400', '600'].includes(c.fontWeight)) bad.add(`${x.tagName}.${x.className} ${c.fontSize}/${c.fontWeight}`); }
    return [...bad]; });
  check(type.length === 0, 'text is 16, 14 or 12 px and weight 400 or 600 ' + JSON.stringify(type));
  await m.locator('#tstatus').evaluate(el => { el.tabIndex = -1; el.focus(); });
  let onMore = false;
  for (let i = 0; i < 40 && !onMore; i++) { await m.keyboard.press('Tab'); onMore = await m.evaluate(() => document.activeElement.matches('.tablet button.more')); }
  check(onMore && await m.evaluate(() => getComputedStyle(document.activeElement).outlineStyle !== 'none'), 'keyboard: Tab reaches the notes control, and its focus shows');
  check(m.errors.length === 0, 'no page errors (0.7.0) ' + m.errors.join(' | '));

  const n = await page(b, 360, 800); await n.setExtraHTTPHeaders(WHO); await n.goto(BASE + 'throne'); await n.locator(`.tablet[data-id="${FRESH}"]`).waitFor();
  check(await noOverflow(n), '360 px: no sideways scrolling');
  const fr360 = await n.locator('[data-f]').evaluateAll(bs => new Set(bs.map(x => Math.round(x.getBoundingClientRect().top))).size);
  const fw = await n.locator('[data-f]').evaluateAll(bs => bs.map(x => Math.round(x.getBoundingClientRect().width)));
  check(fr360 === 1 && new Set(fr).size === 1, `360 px: the four filters fit in one row (widths ${fw.join(', ')})`);
  if (process.env.SHOTS) await n.locator(`.tablet[data-id="${FRESH}"]`).screenshot({path: path.join(process.env.SHOTS, 'card-360-waiting.png')});
  await shot(n, 'throne-360');
  const w = await page(b, 1440, 900); await w.setExtraHTTPHeaders(WHO); await w.goto(BASE + 'throne'); await w.locator('.tablet').first().waitFor();
  if (process.env.SHOTS) await w.screenshot({path: path.join(process.env.SHOTS, 'throne-1440-full.png'), fullPage: true});
  await m.close(); await n.close(); await w.close();
  srv.close();
});
