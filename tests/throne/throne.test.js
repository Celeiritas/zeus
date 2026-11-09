// Zeus · Throne in a real browser, under Elaia, against a fake zeus (tests/throne/fake_zeus.py). tests/throne/run.sh copies
// this file into an argus-ui checkout's tests/ and runs it there, so './lib' is argus-ui's test helper.
// zeus#11 "done means": every field on a request card; a 1 s press sends nothing; a 3 s press approves {id, sha} with who
// is asking; the keyboard hold works; 375 px without sideways scroll; reduced motion draws no ring; every state is an
// icon and a word; an expired row has Check main again. Plus: decline with a reason word, Home's "Needs you", the alert.
// 0.5.0, the decision inbox (docs/design.md §14): Needs your answer above Waiting for your yes, its empty state, an
// option is a tap (pointer, Enter or Space) that sends {id, index}, what a session wrote stays text, Home counts asks,
// api/throne/summary, 375 px and reduced motion.
const {BASE, check, page, center, noOverflow, shot, run} = require('./lib');
const fs = require('fs'), path = require('path');
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
  check(await p.locator('.title h1').textContent() === 'Throne' && /2 deploys waiting · argus-ui 4.8.0 live/.test(await p.locator('#tstatus').textContent()), 'a side page titled Throne; the status line says what waits and what runs');
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
  check(await hist.locator('.hrow', {hasText: 'hermes 0.9.0'}).locator('button.re').count() === 1, 'an expired request is in History with Check main again');
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
});
