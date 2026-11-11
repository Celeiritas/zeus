# Zeus – design: the throne (approved deploys)
Decided 2026-10-21. Zeus is the executor of an Elaia/Argus home setup ([argus-ui](https://github.com/Celeiritas/argus-ui)); it builds on the Hermes design ([hermes](https://github.com/Celeiritas/hermes), `docs/design.md` §4 "Fetching", the deploy-request message) and the Janus design ([janus](https://github.com/Celeiritas/janus), `docs/design.md` §4 `janus verify-approval`). The owner's real allowlist and the approvals journal live in a private config repo; this repo holds code, contracts and examples with placeholders only.

Placeholders follow argus-ui's rule: `{{SERVER}}` (the always-on box that runs Elaia, Zeus, Janus and Hermes's door), `{{PC}}` (a desktop), `{{PHONE}}`, `{{SSH}}` (the ssh alias for the server), `{{HOME_PC}}`, `{{HOME_SERVER}}`. They are filled in on the server from `~/.config/leafy/realm.json`. Examples use `you/<repo>` for repository names and `<base>` for Elaia's URL.

## 0. Decisions
| Question | Choice | Why |
|---|---|---|
| Who has the final say | **The owner, always.** Zeus never merges, never approves, never retries on his own. He runs what the owner merged *and* then approved, and he records it | Nothing runs without a yes. Zeus is the executor; the Throne is where the yes is given |
| Where Zeus lives | **Own repo**: one-shot CLI `~/.local/bin/zeus` + Elaia plugin `throne`. The bolt power (run / restart a task) stays in argus-ui | Agent name = repo name = job prefix (`zeus-`), like Hermes and Janus. Zeus holds keys and runs other repos' deploy scripts; keeping that out of the app keeps Elaia a watcher. And Zeus must deploy argus-ui without redeploying himself |
| What may be deployed | **Only a commit that is on `main` of a repo in `~/.config/zeus/repos.json`**, checked by Zeus himself with `git fetch` + `git merge-base --is-ancestor` at approval time | Hermes's message is a hint, never a source. No branches, no files, no scripts from messages |
| Branch and script | `main` and `scripts/deploy.sh` are **fixed in code**, not in config | A config can't point Zeus at a branch or a different script; a compromised config repo can't widen it |
| Approval identity | `janus verify-approval` on the Throne's POST (`Tailscale-User-Login` + `X-Forwarded-For` behind `tailscale serve`). Fallback: `ssh {{SSH}} 'zeus approve <id>'` from an approved device (`SSH_CONNECTION`) | Janus owns identity; Zeus owns the request, its freshness and the record |
| Fresh approval | The POST carries `id` **and** `sha`; Zeus refuses if the request isn't `pending`, is expired or superseded, or the sha differs from what the page showed | An approval is for one commit, once |
| The yes itself | **Hold to confirm, 3 seconds**, for Approve & deploy and for Roll back (§9) | One tap is too easy on a phone; a dialog that repeats the card is clicked through. A hold is deliberate and still one gesture |
| Signing | Each approval record carries an HMAC-SHA256 (`sig`) over its canonical JSON, keyed by `~/.config/zeus/secret.env` (mode 600). `zeus verify-log` re-checks every line | A signed line can't be edited later without `verify-log` noticing. It is an audit trail, not a second identity check (that's Janus) |
| Memory vs execution | **Zeus writes, Mnemosyne keeps.** Zeus writes his own log; a separate memory job (`mnemosyne-journal`, in the private config repo) carries the monthly approval and deploy logs into that repo. Zeus holds **read-only** keys; the one write key for the journal is Mnemosyne's | One job each: the executor doesn't keep the archive, and the agent that can push to the private repo isn't the one that deploys from it |
| Journal branch | `mnemosyne-journal.timer` (daily) commits `zeus/approvals/*.jsonl`, `zeus/deploys/*.jsonl`, `janus/approvals/*.jsonl` to branch **`journal`** of the config repo; the owner merges it | Nobody pushes `main`. See §4 for what branch protection adds, and what still holds without it |
| Failed deploy | **Auto-rollback** to the last successfully deployed commit when install/restart/health fails; **hold** (nothing changed) when the build fails | The Throne lives inside argus-ui: if a bad argus-ui deploy stays up, the rollback button is unreachable. Rollback re-runs an approval that already exists, so it needs no new one |
| Expiry | A request **expires 72 h** after `seen` and is **superseded** the moment a newer commit on `main` of the same repo arrives. Only the newest commit per repo is approvable; older ones only through Roll back. Expired and superseded requests stay listed in History with a Re-check button | Deploying an older commit of `main` is a downgrade; "fresh" means fresh. Nothing disappears, it just isn't approvable |
| Config deploys | The private config repo is deployed by Zeus like any repo; its `scripts/deploy.sh` validates (`hermes/janus/zeus check-config`) from the checkout **before** copying anything. A request whose diff touches `zeus/repos.json` or `janus/routes.json` carries a **"changes the realm's own rules"** badge | One mechanism, one log. Rules that widen Zeus or Janus deserve a visible flag |
| `hermes release` | **Through Zeus** as action `release`: the Release button on the Delivery page creates and approves a Zeus request with the same 3-second hold (Janus verifies identity, Zeus logs, Hermes releases). Executables stay refused in code even when released | Hermes held the button back until an approval mechanism existed; this is it |
| Repositories built elsewhere | **Staged packs** (§15, 0.6.0): a session builds on `{{PC}}` and stages an immutable pack on the server; Zeus checks the pack and the commit himself and runs one fixed install script on his own copy after the owner's hold | Some builds need files only `{{PC}}` has; the yes, the on-`main` check and the signed log stay the same |
| Idle cost | Zero: `zeus-queue.path` (watches `requests/`) and one-shots started by the page or ssh. Zeus has no timer at all | Resource rule |

## 1. What Zeus is, and isn't
- **One job: run what the owner approved.** Zeus turns an approved request into a checkout of exactly that commit and a run of that repo's `scripts/deploy.sh`, records the result, and can put the previous approved commit back.
- **He never fetches from anywhere but the allowlisted repos**, never runs a command from a message, a page or a config, never deploys a branch, never touches a file Hermes carried, never pushes anywhere. The only code Zeus runs that he didn't ship with is `scripts/deploy.sh` of a commit the owner merged and then approved.
- **Nothing of Zeus runs while idle.**
- **Python 3 standard library only**, one zipapp at `~/.local/bin/zeus` (like Hermes and Janus). Calls `git`, `janus`, `hermes`, `systemd-run`, `systemctl --user`, `tailscale`. Subcommands: `queue` (process `requests/`), `request`, `approve`, `approve-release`, `decline`, `deploy-run` (internal, under `systemd-run`), `rollback`, `status`, `check-config`, `init`, `verify-log`, `seen`, since 0.5.0 the decision inbox: `ask`, `answer`, `asks` (§14), and since 0.6.0 `stage-request` (§15).

## 2. `~/.config/zeus/repos.json` – the allowlist
The owner's copy lives in the private config repo (`zeus/repos.json`), copied (not symlinked) into `~/.config/zeus/` by that repo's deploy. This repo ships `examples/repos.example.json` with placeholders; the loader expands `~` and `{{PLACEHOLDERS}}` from `~/.config/leafy/realm.json`.

```json
{
  "version": 1,
  "expire_hours": 72,
  "deploy_timeout_s": 900,
  "health_wait_s": 30,
  "repos": {
    "you/argus-ui": { "url": "https://github.com/you/argus-ui.git",
                      "path": "~/src/argus-ui", "health": { "http": "http://127.0.0.1:8095/api/health" },
                      "services": ["argus-ui"] },
    "you/hermes":   { "url": "https://github.com/you/hermes.git",
                      "path": "~/src/hermes", "health": { "exec": "hermes check-config" } },
    "you/janus":    { "url": "https://github.com/you/janus.git",
                      "path": "~/src/janus", "health": { "exec": "janus check-config" } },
    "you/zeus":     { "url": "https://github.com/you/zeus.git",
                      "path": "~/src/zeus", "health": { "exec": "zeus check-config" } },
    "you/realm":    { "url": "git@github-realm-zeus:you/realm.git",
                      "key": "~/.config/zeus/keys/realm", "path": "~/src/realm",
                      "private": true, "rules": ["zeus/repos.json", "janus/routes.json", "janus/doors.json", "hermes/routes.json"],
                      "health": { "exec": "janus check-config" } }
  }
}
```
| Field | Meaning |
|---|---|
| key (`owner/name`) | Must equal the last two path parts of `url`; `url` must be `https://github.com/<owner>/<name>.git` or an ssh alias from `~/.ssh/config` for a private repo. Anything else fails `check-config` |
| `path` | The clone on `{{SERVER}}`. Zeus creates it on first deploy, **detached HEAD only**, `git -c core.hooksPath=/dev/null`; nobody edits here |
| `key` | Read-only deploy key for a private repo, Zeus's own (Hermes has a separate one for decks: one home per secret, one key per agent) |
| `health` | One of: `{ "http": "http://127.0.0.1:<port>/..." }` (localhost only, expects 200 and, when the body has a `version`, that it equals the commit's `VERSION`), `{ "service": "<unit>" }` (`systemctl --user is-active`), `{ "exec": "<agent> check-config" }` (**from a fixed list in code**: `hermes check-config`, `janus check-config`, `zeus check-config`). Never a free command |
| `services` | Units Argus should watch after this deploy; shown on the card |
| `rules` | Paths whose change flags the request "changes the realm's own rules" |
| `kind`, `staging`, `install` | Since 0.6.0: `kind` `staged-pack` for a repository whose pack is built elsewhere, with its fixed staging folder and fixed install script (§15). Absent = `git`, everything in this section |

Not in config, on purpose: branch (always `main`), deploy script (always `scripts/deploy.sh` of the checked-out commit), who may approve (Janus's `approvers.zeus`).

**Bootstrap, once by hand:** install the Zeus zipapp, `zeus init` (makes `secret.env`), copy `repos.json`, make Zeus's read-only deploy key for the config repo and add it on GitHub. Mnemosyne's write key for the journal is made at the same time (§4). After that Zeus deploys everything, himself included (a zipapp replaced on disk doesn't disturb the running one-shot).

## 3. The request record
Hermes's `hermes-github` writes `<repo>-<sha7>.json` into `~/.local/share/zeus/requests/` (route `deploy-request`, checked by Janus's `deploy-request-fields`). `zeus-queue.path` (`DirectoryNotEmpty=`) starts `zeus-queue.service`, a one-shot that turns each file into a record under `~/.local/share/zeus/queue/<id>.json` and leaves `requests/` empty (the same rule as Hermes's `in/`: every arrival leaves, or the path unit fires forever).

```json
{ "id": "20261021-090012-argus-ui-3fa9c1b", "action": "deploy", "state": "pending",
  "repo": "you/argus-ui", "sha": "<40 hex>", "previous": "<40 hex>",
  "subject": "Mentor: Learn phase", "version": "4.9.0", "checks": "success",
  "changelog": ["- Learn cards …"], "files": 12, "rules_changed": [],
  "seen": "2026-10-21T09:00:00-06:00", "expires": "2026-10-24T09:00:00-06:00",
  "verified": { "on_main": true, "at": "2026-10-21T09:00:12-06:00" },
  "approval": null, "deploy": null }
```
- `verified.on_main` is **Zeus's own check**: `git fetch origin main` into the clone (or a bare mirror before the first deploy), then `git merge-base --is-ancestor <sha> origin/main`. Not on `main` → state `refused`, never shown as approvable. `files` and `rules_changed` come from `git diff --stat previous..sha` in that same fetch.
- **States:** `pending` → `approved` → `deploying` → `deployed` | `failed` | `rolled-back`; or `expired`, `superseded`, `declined`, `refused`. Terminal records move to `queue/.done/` (never deleted; a cleaning job may trim them after six months, preview first). The History section lists everything in `.done/`, so an expired request is still visible, with **Re-check** to ask for a fresh one.
- **Expiry** is enforced where it matters: at approval time, and lazily when the page lists the queue. No timer.
- **Superseding:** when `zeus-queue` records a new commit for a repo that already has a `pending` one, the older becomes `superseded` (its card says "replaced by 4.9.1").
- `zeus request <repo>` makes a record now for the current `main` without waiting for Hermes's hourly check (the Throne's **Re-check** button runs `hermes-github` and then this).

## 4. Approval
**From the Throne page (normal path).** The button is held for 3 s (§9), then `POST api/throne/approve { "id": "…", "sha": "…" }` → the plugin runs `zeus approve <id> --sha <sha> --user <Tailscale-User-Login> --ip <X-Forwarded-For>`. Zeus:
1. loads the record; refuses unless `state = pending`, `now < expires`, `sha` matches, and `verified.on_main` is true **re-checked now** (one more `git fetch`);
2. runs `janus verify-approval` with `{ "action": "deploy", "repo", "sha", "user", "ip", "when": now }` on stdin — exit 0 allowed · 10 refused (reasons shown on the card) · 2 failed (held, Argus glares);
3. writes the approval into the record and appends a signed line to `~/.local/share/zeus/log/approvals-YYYY-MM.jsonl`:
   ```json
   { "t": "…", "id": "…", "action": "deploy", "repo": "you/argus-ui", "sha": "…", "version": "4.9.0",
     "by": { "user": "<tailnet login>", "node": "{{PHONE}}", "ip": "100.x.y.z", "via": "elaia" },
     "janus": "allowed", "zeus": "0.1.0", "sig": "<hmac-sha256 hex>" }
   ```
   `sig` covers the line without `sig`, keys sorted, no spaces. Key: `ZEUS_LOG_KEY` in `~/.config/zeus/secret.env` (32 random bytes, made by `zeus init`, covered by the server's backup job, never in git).
4. starts the deploy **detached**: `systemd-run --user --unit=zeus-deploy-<id> --collect -p CPUQuota=50% -p MemoryMax=1G -p IOWeight=50 zeus deploy-run <id>` — because `deploy.sh` restarts argus-ui, which is the parent of the plugin's subprocess; a deploy run inside the request would die halfway through its own install.

**From ssh (fallback, and the new manual line).** `ssh {{SSH}} 'zeus approve <id>'` or `ssh {{SSH}} 'zeus deploy argus-ui'` (= `request` for current `main` + `approve`, one line). Identity: `SSH_CONNECTION` gives the client address; Zeus passes `via: ssh` and the address, Janus does the same whois check (the node must be in `approvers.zeus.nodes`). No `--user` flag is accepted from the shell: Janus takes the user from whois. The shell asks "Deploy argus-ui 4.9.0? type yes" instead of a hold.

**The line Zeus prints after a yes** names who approved, as the signed line has it: `<id>: approved (you/argus-ui 4.9.0) by <login>; deploying as zeus-deploy-<id>`. Over ssh that login is the one Janus read from whois (nobody types a name there). If Janus allowed the approval without naming a login, the line says what he did check and that the name is missing: `… from <device> (Janus named no login); …`, or the client address when he named no device either. Zeus never prints the shell's user in that place: it is the server's account, not who Janus verified, and the screen must agree with the signed line, whose `by.user` is null in that case. A rollback's line follows the same rule: it already named the signed login, and since 0.6.1 it has the same no-login wording. A login or device name that is empty or only spaces counts as none.

**Decline:** `POST api/throne/decline { id, reason }` / `zeus decline <id> --reason <word>`: state `declined`, logged, no Janus call (declining changes nothing).

**The journal (Mnemosyne).** A daily one-shot in the private config repo (`mnemosyne-journal.timer`, `Persistent=true`) copies the monthly files `~/.local/share/zeus/log/approvals-*.jsonl`, `deploys-*.jsonl` and `~/.local/share/janus/log/approvals-*.jsonl` into a clone of the config repo (`zeus/approvals/`, `zeus/deploys/`, `janus/approvals/`), commits when something changed, and pushes branch `journal` with its own write deploy key. The owner merges `journal` into `main`. `zeus verify-log` can run on the repo copy too: the signatures travel with the lines.
- **Branch protection.** GitHub's "require a pull request before merging" on `main` is available on private repos only with a paid plan. If it's available: turn it on, and then the journal key can never change what gets deployed. If it isn't: the design still holds, because **Zeus deploys only what the owner approves on the Throne**, every config-repo request shows the files and the commit author, and the rules badge marks a change to the realm's own configs. The journal key is the only write key on the server and belongs to Mnemosyne, not to the agent that deploys.

**Janus contract additions:** `action` may be `deploy`, `rollback` or `release`; for `release` the record carries `"arrival": "<hermes id>"` instead of `repo`/`sha`. Janus logs all three in its own `approvals-*.jsonl` (the door's log); Zeus's signed log is the decision record. Two logs, two owners, no duplication of judgement.

## 5. The deploy (`zeus deploy-run <id>`)
One deploy at a time (`flock ~/.local/share/zeus/lock`). Log to `~/.local/share/zeus/log/deploy-<id>.log` (the page tails it) and one JSON line to `log/deploys-YYYY-MM.jsonl` at the end.
1. **Fetch:** `git -C <path> fetch origin main` (private: `GIT_SSH_COMMAND='ssh -i <key> -o IdentitiesOnly=yes'`). Verify again `merge-base --is-ancestor <sha> origin/main`.
2. **Checkout:** `git checkout --detach <sha>`; submodules are not used anywhere, so refused if present.
3. **Run:** `bash scripts/deploy.sh --no-pull` in `<path>`, with `ZEUS_ID`, `ZEUS_SHA`, `ZEUS_PREVIOUS` in the environment, timeout `deploy_timeout_s`. The script owns build + install (argus-ui's keeps sounds, weather and bind; previous version set aside, never deleted). argus-ui 4.9.1 has `--no-pull` and prints `ZEUS_STAGE=install` before its install; `examples/deploy.sh.example` shows what any repo's script must honour.
4. **Health:** after the script exits 0, wait up to `health_wait_s` for `health` to pass. For argus-ui: `GET http://127.0.0.1:8095/api/health` → `{ "ok": true, "version": "4.9.1", "uptime_s": 2 }`; the version must match.
5. **Record:** `state/<repo>.json` → `deployed: {sha, version, at, approval}`, the old `deployed` becomes `previous`, `history` keeps the last 10. Request state `deployed`. Alert file cleared.

Why re-deploy a commit rather than restore a set-aside folder for rollback: a commit is reproducible and the same path for every repo; set-aside folders stay as argus-ui's own safety net.

## 6. When a deploy fails
| Where it failed | Zeus does | State |
|---|---|---|
| fetch / sha not on main / checkout | nothing changed; stop | `failed` (reason on the card) |
| `deploy.sh` exits non-zero **before** `ZEUS_STAGE=install` (argus-ui's build runs first) | nothing changed; stop | `failed` — **hold** |
| `deploy.sh` fails after `ZEUS_STAGE=install`, times out, or health never passes | **auto-rollback**: checkout `previous`, run `scripts/deploy.sh --no-pull`, health again | `rolled-back` if the previous comes up; `failed` + alert if it doesn't |
| the second deploy also fails | stop; alert `~/.local/state/argus-ui/alerts/zeus.json` (`"message": "argus-ui 4.9.0 failed and 4.8.0 could not be restored"`, `"link": "throne"`); Argus glares if it's alive | `failed` |
A failed request is never retried on its own. Re-check makes a new request for the same commit once the fix is merged (a new commit) — the old one is `superseded`.

**Roll back by hand** (a 3-second hold on the Throne, or `ssh {{SSH}} 'zeus rollback argus-ui'`): action `rollback`, target = `state.previous`, Janus verifies the identity, Zeus logs a signed line, same deploy-run. Needs no fresh deploy approval: the target was approved and ran before. Rolling back twice goes back only one step further (history is a stack; the page shows what the target would be, e.g. "→ 4.8.0").

## 7. Config deploys and `hermes release`
**The private config repo** gets its own `scripts/deploy.sh` (private, so it may hold real paths): `hermes check-config`, `janus check-config`, `zeus check-config` **against the checkout's files** (each tool accepts `--file <path>`); only when all pass, copy `hermes/routes.json`, `hermes/authorized_keys.hermes` (through `hermes keys sync`), `janus/routes.json`, `janus/doors.json`, `zeus/repos.json`, `leafy/realm.json`, job units into place (copy, never symlink), then `systemctl --user daemon-reload`. A failed check means nothing is copied (hold). The Throne card lists the files changed and, if any `rules` path is among them, the badge "changes the realm's own rules" with the diff of those files inline, so the owner reads what they're widening before they hold. The new `repos.json` takes effect from the next request, not for the one being deployed.

**`hermes release`.** The Delivery page's quarantine detail gets a **Release** button (same 3-second hold): `POST api/delivery/release { arrival, reason }` → the plugin calls `zeus approve-release <arrival> --reason <word> --user … --ip …` → Zeus writes a `release` record, Janus verifies the identity (`action: release`), Zeus logs a signed line and runs `hermes release <arrival> --reason <word> --approval <zeus id>`. Hermes keeps refusing executables in code; a release overrides route, kind, shape and size rules only. The Throne lists releases in its history (and pending ones if the Delivery page's call failed halfway). Discard stays a CLI line (it moves, never deletes; nothing to approve).

## 8. What replaces the manual deploy line
| Before Zeus | After Zeus |
|---|---|
| `ssh {{SSH}} 'cd ~/src/argus-ui && bash scripts/deploy.sh'` | Hold **Approve & deploy** on the Throne (from `{{PHONE}}` or `{{PC}}`), or `ssh {{SSH}} 'zeus deploy argus-ui'` from `{{PC}}` — same identity check, same log |
| watch the ssh output | the card tails `deploy-<id>.log`; Argus watches `zeus-deploy-<id>` and then `argus-ui` |
| remember which version is live | **Deployed now** on the Throne: version, sha7, when, by whom, Roll back → previous |
| `git pull` inside `deploy.sh` | `deploy.sh --no-pull` (argus-ui 4.9.1). Run without the flag it still pulls, for the bootstrap and for repos without Zeus. The clone is detached from then on, so a hand-run `git pull` there fails on purpose: the clone is Zeus's |

## 9. Zeus · Throne (Elaia plugin `throne`)
A `server` plugin shipped in `plugin/`, installed to `~/.config/argus-ui/plugins/throne/`, pill "Throne" with the subtitle "Zeus · deploys" (`name` and `subtitle` in `plugin/plugin.json`), URL `<base>/throne` (`/zeus` is Zeus's task page in Watch; persona keys are reserved ids). Colour: storm gold `#E0B341` on Night. Zeus's bolt medallion is drawn by the page already. Argus small in the corner, glaring while `alerts/zeus.json` exists.

**Page, laid out for a phone first (375 px), list pattern:**
0. **Needs your answer** (since 0.5.0, §14): the questions sessions asked the owner, above everything that waits for a yes.
1. **Status line:** "1 deploy waiting · argus-ui 4.8.0 live since Tue". Feeds Elaia's Home page through the plugin `status` endpoint.
2. **Waiting** (the queue, newest first): one card per request: repo, version, sha7 (link to the commit), subject, changelog lines, checks chip (✓ passed · ✕ failed · – none), "12 files", the rules badge when it applies, expiry ("expires in 2 d"). Buttons: **Approve & deploy** (hold), **Decline** (asks for a reason word), **Re-check**.
3. **Deploying:** the live log tail (`aria-live="polite"` for stage changes only, not every line), stage chips (fetch → checkout → build → install → health), Cancel is **not** offered (half a deploy is worse than a whole one; rollback is).
4. **Deployed now:** per repo: version, sha7, when, by whom (user · device), **Roll back → 4.8.0** (hold, same identity check).
5. **History:** everything in `.done/` plus deploys (the page gets the newest 60 rows; "As built" below), newest first, with outcome chips (✓ deployed · ↩ rolled back · ✕ failed · ⌛ expired · ↑ superseded · ⊘ declined · ⇥ released), each opening its log; expired and superseded rows carry **Re-check**. Filters: All · Waiting · Expired · Failed.

**Hold to confirm (Approve & deploy, Roll back, Release).** Press and hold for 3 s: a ring around the button fills while the label counts "Hold… 3 · 2 · 1"; letting go early cancels with nothing sent; at 3 s the POST goes and the label turns to "Deploying". Keyboard: hold <kbd>Space</kbd> or <kbd>Enter</kbd> on the focused button for the same 3 s (a key-repeat timer, cancelled on keyup). Reduced motion: no ring, the countdown words only. Screen readers: the button is described by "Hold for three seconds to deploy argus-ui 4.9.0", and the status line announces the result. Touch target ≥ 44 px; the hold never needs a path gesture, so WCAG 2.5.1 (pointer gestures) is met.

**API:** `GET api/throne`, `api/throne/status` and (0.5.0) `api/throne/summary` read `queue/`, `state/` and the logs directly (no subprocess on reads; `deploy-<id>.log` tail is a file read). POSTs pass Elaia's checks (same-origin, JSON, 4 KB): `approve`, `decline`, `rollback`, `recheck` (runs `zeus request <repo>`; "As built" below), `seen`, and (0.5.0) `answer { id, index }` → `zeus answer <id> --index <n> --user … --ip …`. Every POST that changes what runs calls Zeus, and Zeus calls Janus; the plugin itself never decides.

**Accessibility (argus-ui `CLAUDE.md` + WCAG 2.1 AA):** keyboard path through every card and button with visible focus; no sideways scroll at 375 px, targets ≥ 44 px; state = icon + word, never colour alone; with reduced motion no lightning on approve (a static bolt icon and the word "Deploying"); one `aria-live` region on the status line.

**Why it's built this way.** The request card shows exactly what will run (repo, commit, changes, tests) before the one irreversible gesture, and the result appears where the gesture happened (visibility of system status; Nielsen 1994). Rollback is one hold because the cost of a bad deploy is paid by the same page you'd use to fix it. The hold is the friction, not a dialog: a confirm that repeats the card's text would be clicked through (Akhawe & Felt 2013), while three seconds with the card still in view is time to read it.

**As built in 0.4.0** (`plugin/`, issue #11):
- The page uses argus-ui's shared `holdConfirm` and `fx` (argus-ui 4.18, `docs/fx.md` there): the hold, the bolt on an approval and the crumble on a decline come from Elaia, so the Throne can't drift from the other pages. A click that cannot be held (a screen reader's or a switch's) arms the button and a second click within 5 s confirms.
- Reads change nothing: a waiting request past its expiry is *shown* as expired, and Zeus records it when he next lists the queue or is asked to approve it. `Re-check` is labelled **Check main again** and runs `zeus request <repo>` (Zeus fetches `main` himself); asking `hermes-github` first is left to Hermes's hourly check.
- The rules badge names the changed rules files; the inline diff of those files is not built yet (the record carries only their names).
- The tablet, throne and stage-node art waits for the owner's approval (argus-ui `docs/ui-plan.md` §13); until then the page uses plain cards and chips.
- Tests: `tests/test_throne_plugin.py` (reads, stages, Home's status, the exact `zeus` arguments, refusals) and `tests/throne/throne.test.js` in a browser under Elaia against `tests/throne/fake_zeus.py` (`bash tests/throne/run.sh <argus-ui checkout>`; the `throne-page` CI job).

**As built: where the page still differs from the text above** (checked against the code at 0.6.1; issue #11 stays open for the first point):
- **No inline diff of rules files.** The badge names the files; nothing shows what changed in them.
- **The hold's name for a screen reader** is the button's `aria-label`, "Deploy argus-ui 4.9.0 (hold 3 seconds to confirm)", set by Elaia's `holdConfirm`; there is no separate "Hold for three seconds to deploy …" description. While the POST runs the label reads "Working…", not "Deploying".
- **The approve button** is labelled with what it deploys, "Deploy argus-ui 4.9.0", not "Approve & deploy" (`plugin/page.js`).
- **History** is the newest 60 finished records (`HISTORY` in `plugin/plugin.py`); older ones stay in `queue/.done/` and are not shown. A log opens with its last 80 lines.
- **Two live regions, not one:** the status line is a `role="status"` paragraph, and stage changes are announced by a second, visually hidden `aria-live="polite"` line.
- **Deployed now** names who approved only when the approval carries a login ("by <login> · <device> (<via>)"); an approval Janus allowed without naming a login shows no name there, while the command line says "from <device> (Janus named no login)" (§4).
- **What the line says (0.7.0):** "argus-ui 4.36.0 running" when the program's `health.http` answers with its version, else "argus-ui 4.33.0 deployed" (Zeus's own record); never "live". The design's "since Tue" is not in the line.
- **The ring, the countdown words, the early-release cancel and the Space/Enter hold are Elaia's code** (`web/js/hold.js` in argus-ui). This repository's browser test proves them on the page (a 1 s press sends nothing, a 3 s press approves, a held Space rolls back, no ring with reduced motion), but a change to the hold itself is made and tested in argus-ui.

**As built in 0.7.0: readable on a phone** (the owner's field audit of 2026-11-11: the page said "4.33.0 live" while 4.36.0 ran, Deploy sat 2000 px down under raw markdown, Deploy, Decline and Roll back stood together without words):
- **What really runs.** On every read the plugin asks each repository's `health.http` address (only `http://127.0.0.1:<port>/…`, re-checked; GET, no redirect, 1 s, 64 KB, kept 30 s; a raw socket to the literal address, so no name lookup and no proxy) for its JSON `version`. Each Deployed now row gets `running` and `drift` (`same`, `newer`, `older`, `no-record`, `unknown`; `docs/contract.md` §3c). Elaia's server is a `ThreadingHTTPServer` and a plugin's GET runs outside its POST lock, so asking Elaia's own `api/health` from inside her handler cannot block her; the 1 s limit bounds it anyway. A POST never asks.
- **Old requests.** A waiting request whose version is older than what runs is `stale` (an equal version is not: a fix after review often keeps it). Its card sits under a small heading "Old requests" (14 px, 600) after the cards that need a yes, says first "Older than what runs now (4.36.0)", offers Decline only, and it is not counted in Home's "needs you" or `summary`; the line says "1 old request, expires Fri". Zeus himself still treats it as pending until it expires (a proposal for the gate is in the pull request of 0.7.0).
- **The waiting card, top down:** name and version; checks, files and expiry on one line; **Deploy** (the hold) full width with one sentence: "Zeus installs it, then checks that it works. If the check fails, he puts 4.33.0 back." (it names his own last deploy, and says so when that is not what runs; when the request is the very commit he deployed last, `restore()` in `zeus/deploy.py` has nothing to put back, so it says "This is the version Zeus deployed last. He installs it again, then checks that it works. If the check fails, there is nothing for him to put back."); **Decline** on its own row, quieter, with "Nothing is installed. The request closes."; "Read the notes (N lines)" (`aria-expanded`) folds the subject, the commit link (44 px) and the notes; notes longer than 20 lines end with a second "Hide the notes" that returns focus to the first. The notes are escaped first, then headings, bullets, bold and code spans become plain styled text; a markdown link keeps its words and is no link.
- **Deployed now, in words:** "Runs now: 4.36.0" (or "Last deployed by Zeus: 4.33.0" when nothing answers) and, only when the record differs, one sentence ("Zeus's own last deploy was 4.33.0; the newer version was installed some other way."). While Zeus deploys that program, what runs moves ahead of his record until the health check passes, so the row then says only "Zeus is deploying a new version now." Roll back keeps its hold and says "Zeus puts 4.32.0 back in place of 4.36.0, then checks that it works."; when `drift` is `newer`, `older` or `no-record` it sits folded behind "Other actions". With `unknown` it stays in view (no evidence that the record is wrong). **Check main again** appears once per program, here only: "Looks for a newer version. Installs nothing." The commit link left this row.
- **History:** what happened (the waiting requests are no longer repeated under All), the newest 5 rows, then "Show older (N)". A row's only control is its log. The four filters stay in one row at 360 px. After Enter on a filter or on Show older, focus stays on that control (the redraw found it again only inside a card before).
- **Measured** (tests/throne/throne.test.js): every visible control at least 44 x 44 px; text 16, 14 or 12 px at weight 400 or 600 (section headings went from 18 to 16 px); no sideways scroll at 360 and 375 px; a section with nothing in it is one plain line; the two live regions and reduced motion are unchanged.

## 10. Resource use on the server
| When | What runs | Cost |
|---|---|---|
| Idle | nothing (`zeus-queue.path` is a kernel watch; sshd and systemd already run) | 0 |
| A request arrives | `zeus-queue`: one `git fetch` (a few KB), a diff stat | ~1 s |
| An approval | `zeus approve` + `janus verify-approval` + one `tailscale whois` | ms |
| A deploy | `zeus-deploy-<id>`: the repo's build + install, under `CPUQuota=50%`, 1 GB | argus-ui: ~20 s |
| Daily (Mnemosyne) | `mnemosyne-journal`: copy, `git commit` + push if a log changed | seconds; nothing when it hasn't |
| Disk | queue records ~1 KB each; a log line ~300 B; `deploy-<id>.log` a few KB | — |

## 11. Public vs private
**This repo (public, MIT), same tree as Hermes and Janus:**
```
zeus/         cli (the command line), config (repos.json), init (secret.env, the data folders), queue (records, the on-main check),
              commands (request, decline, status), approve (janus call, typed yes over ssh, the signed line), deploy (git,
              health, hold vs rollback), rollback, staged (staged packs, §15), asks (the decision inbox, §14), alert (the Argus
              alert file), log (monthly JSONL, hmac), verify (verify-log)
plugin/       Elaia server plugin "throne": plugin.json, plugin.py, page.js (the hold is Elaia's shared holdConfirm), page.css
units/        zeus-queue.path/.service, argus-limits drop-in, agents/ (Argus's task-card notes for zeus-queue and zeus-deploy)
examples/     repos.example.json (placeholders), deploy.sh.example (what a repo's deploy script must honour: --no-pull,
              ZEUS_STAGE=install), install-pack.sh.example (a staged pack's install script, §15.5)
docs/         design.md (this doc), contract.md (Hermes → Zeus message, Zeus ↔ Janus, plugin API), deploy-script.md
scripts/      build.sh (zipapp), install.sh, check-private.sh, install-hooks.sh (Zeus has no deploy.sh of his own here:
              `scripts/deploy.sh` is what each deployed repository carries)
tests/        run.sh (unittest): a fake git remote (local bare repos), a fake janus, a fake systemd-run; cases for every state
              transition, expiry, supersede, rollback, hmac verify; throne/ is the page in a browser under Elaia, against a fake zeus
.github/workflows/  tests.yml (the build, the unit tests, the Throne page in a browser), check-private.yml
CLAUDE.md, README.md, VERSION, CHANGELOG.md, LICENSE
```
**The private config repo:** `zeus/repos.json`, `scripts/deploy.sh`, the journal (`zeus/approvals/`, `zeus/deploys/`, `janus/approvals/`, via branch `journal`), `mnemosyne-journal.service/.timer`. **Never in any repo:** `secret.env`, deploy keys, `queue/`, `state/`, `deploy-*.log`.

## 12. What other repos need
| Repo | Change | Status |
|---|---|---|
| argus-ui | `GET api/health` and `scripts/deploy.sh --no-pull` + `ZEUS_STAGE=install` | 4.9.1 (PR #9) |
| argus-ui | the `server` plugin kind (Mentor's move; Hermes, Janus and the Throne all wait on it); alert files | existing issues |
| hermes | turn on the `deploy-request` route; `hermes release --approval <id>` and `hermes keys sync <file>`; `hermes check-config --file`; the Delivery page's Release button (hold) calling Zeus | filed |
| janus | `action` ∈ deploy · rollback · release, `arrival` for release (on the `verify-approval` issue); `janus check-config --file` | filed |
| argus-ui (root actions, §16) | slices A1 to A3: the needs-yes path for a tier R ask with a plain `noopener` link; a page for the registered keys as labels (no ids, no setup link); buzzes from root's `attention.jsonl`, the minute-5 reminder and summary counts | planned |
| hermes (root actions, §16) | Taildrop in and out without Tailscale's operator, ready before `zeus-root` is installed | filed |
| private config repo | `zeus/repos.json`, `scripts/deploy.sh`, `mnemosyne-journal` job + write key, branch protection if the plan allows | the owner |

## 13. Build order
Where it stands (2026-11-09, version 0.6.1): 0.1 to 0.6 are built and merged (0.6.1 changes two printed lines and these notes). "Built" below means built in code and covered by the tests. This section does not record which release a server runs (`zeus --version` there says); what the owner has done on the server is the 1.0 row.
- **Not built:** `approve-release` (#12; the command is a stub that says so, and there is no Release button); the inline diff of rules files on the Throne (§9, "As built"); tiers 1 and 2 of §14 (a proposal the owner has not confirmed) and the power actions of 0.7 that wait on them.
- **Built differently from the text above:** the Throne; §9's "As built" lists say where the page differs from the rest of §9.

| Version | Issues (one branch, one PR each) | Needs | Status |
|---|---|---|---|
| 0.1 "the queue" | #2 skeleton, config loader, `check-config`, `init`, zipapp build · #3 `zeus-queue`: record, on-main check, supersede, expiry · #4 `request` / `decline` / `status` CLI | Hermes message format (done) | built |
| 0.2 "the yes" | #5 `approve` (freshness, Janus call, signed log) · #6 ssh approval path (`SSH_CONNECTION`, typed yes) · #7 `verify-log` | janus `verify-approval` | built |
| 0.3 "the deploy" | #8 `deploy-run` under `systemd-run`: fetch, checkout, `--no-pull`, health · #9 hold vs auto-rollback, `rollback`, `state/` · #10 alert file | argus-ui 4.9.1 | built |
| 0.4 "the throne" | #11 Throne plugin page (list, hold-to-confirm, live log, History with filters, a11y tests) · #12 `approve-release` + Delivery page button | argus-ui `server` plugin kind; hermes `release --approval` | #11 built (0.4.0 to 0.4.4) except the inline diff of rules files (§9, "As built"); #12 not built (a stub that says so) |
| 0.5 "the inbox" | §14 tiers (proposed) · the decision inbox: `ask` / `answer` / `asks`, Needs your answer on the Throne, `api/throne/summary` | nothing new | the decision inbox is built (0.5.0); tiers 1 and 2 are still a proposal |
| 0.6 "the staged pack" | staged-pack deploys for a repository whose build needs files only `{{PC}}` has: §15, `zeus stage-request`, kind `staged-pack`, the fixed install script, the Throne's pack card | §14 | built (0.6.0) |
| 0.7 | power actions as tier 2 (`zeus power gamemode\|desktop\|reboot`) | §14 tier 2 confirmed | planned |
| root actions (after 0.7, own milestone) | §16 slices Z1 (docs) to Z6, with argus-ui A1 to A3; every slice under `root/` never auto-merges, gets a hostile fresh review and the owner's yes | §14 tier R; Tailscale's operator removed before the install; Hermes's Taildrop without it | Z1 only (docs); nothing under `root/` built |
| 1.0 | #13 bootstrap on the server, config-repo `deploy.sh`, first approved deploy of argus-ui, retire the manual line | all above; the journal job | partly: Zeus is installed on the server and argus-ui has been deployed through him with approvals over ssh (seen 2026-11-09); #13 stays open until its two "Done means" boxes are ticked; the journal job is Mnemosyne's |

## 14. Tiers
**Status: proposed 2026-11-09, the owner to confirm the second tap for tier 1.** Tier 3 (decisions) is built in 0.5.0 because it changes nothing Zeus runs; tier 0 is today's behaviour, unchanged; tiers 1 and 2 change nothing until the owner confirms them. Tier R (§16) is designed, not built.

Every request Zeus shows has a tier. The tier sets the gesture on the Throne, how long the request stays approvable, what Janus is asked, and it is written into the signed log line.

| Tier | What | The gesture | Expiry | Janus (`verify-approval`) |
|---|---|---|---|---|
| **0 · review** | a deploy (or rollback) of an allowlisted repository's `main` commit that changes none of the realm's rules | the 3-second hold (§9, today) | 72 h (`expire_hours`) | `action: deploy` or `rollback`: the user and the node must be in `approvers.zeus` (today) |
| **1 · rules** | a deploy whose diff touches a `rules` path of `repos.json` (the gold badge "Changes the realm's own rules": `CLAUDE.md`, Zeus/Janus/Hermes config, units) | the 3-second hold **plus a second tap, "I read the changed rules"**, which only appears after the rules files' names (later their diff) have been shown. Recommended over typing the repository name: typed confirmations are the owner's own, over ssh, and typing on a phone invites copy-paste | 24 h: rules deserve a fresh read | as tier 0, plus `"tier": 1`, so Janus can (later, optional) require a narrower list `approvers.zeus.rules`; absent, the same people as tier 0 |
| **2 · power** | reboot, a session change (to game mode or to the desktop), anything that interrupts a game or stops services | the 3-second hold; **refused while a game runs** unless a second hold, "A game is running. Do it anyway?". It mirrors the reboot gate of the server's operations notes and never weakens it | 10 min: a power action is about now | `action: power`, `unit` from a fixed list in code; the user and node as tier 0 |
| **R · root** (proposed, §16) | a listed change that needs root on `{{SERVER}}` (a firewall rule, a service restart), asked by a session with `zeus root-ask <id>` | not a hold on the Throne: a tap on the **key page**, on its own port, then the fingerprint, signed for that one action, the key page's exact origin and one moment | per entry: 24 h for network changes, 1 h for restarts; the challenge itself 180 s | not asked. Janus is not involved: the root-side runner verifies the signature itself (§16.4). Zeus's ask is data and starts nothing |
| **3 · decisions** | a plain question with options, asked by a session, answered by the owner; **no side effect in Zeus** | one tap on an option (a choice, not a hold: nothing runs) | set by the asker, 10 min to 30 d, default 3 d | not asked: like Decline, an answer changes nothing that runs. The Throne passes who tapped (`tailscale serve`'s headers) and Zeus records it |

**Tier R** (root actions from the phone, §16) sits beside the others, not above them. Elaia's people and roles do not matter for it, so "every login counts as the owner" is harmless there. **The approver recorded** is the key (its key ref, its action, the device it was registered from, the check words of its sitting), plus the login Serve passed, marked "seen, not proof". The line is root's own log line (`docs/contract.md` §8), not Zeus's signed approval line.

**How a tier is chosen: by Zeus, from what the record is, never from a message.** A deploy is tier 0, or tier 1 when Zeus's own `git diff` (§3) touches a path in that repository's `rules` list in `repos.json`; a `tier` field in a Hermes message, a page's POST or anywhere else is ignored. A power action is tier 2 because of the command (`zeus power …`, from a fixed list of units in code). A decision ask (`zeus ask`) is always tier 3, and its record has no field Zeus acts on: no repository, commit, unit, path or command. A root ask (`zeus root-ask`, §16) is tier R, a different command: its record names one id from `/etc/zeus-root/actions.json`; Zeus reads that id only to copy the title, set the expiry and refuse an id that is not in the list, and nothing else in the record is read. It never carries a path, command or script, and it can make nothing run.

**The log record** is the same signed line as §4, plus `"tier"`. In 0.5.0 the new lines (`ask`, `answer`, `ask-expired`) carry it; the deploy, rollback and decline lines gain it when tiers 1 and 2 are built, so nothing that exists today changes shape before the owner confirms.

**What can a hostile message do?** Nothing it couldn't before.
- A deploy-request message still only makes a record that Zeus verified (allowlisted, on `main`, his own fetch), and it cannot lower a tier: the tier comes from Zeus's own diff against the allowlist file.
- An ask can put a question in front of the owner, nothing more. Making one needs a shell on `{{SERVER}}` as the owner's user (the same account that could already run `zeus`). Its answer is a string in a signed record; Zeus never runs anything because of it, and a session that reads it must treat it as data (the rule for sessions below). The worst case is a misleading question; the card shows which project asked, keeps everything the session wrote as plain text, and sits in its own section with no hold button, so it can't pass for a deploy.
- An answer can't be changed after the fact without it showing: the record and the log line are signed (`ZEUS_LOG_KEY`), the first answer wins, a second one is refused, and a record edited outside Zeus is refused rather than answered, left out of `zeus asks` (the read path sessions use; it names the record on stderr) and named by `zeus verify-log`.
- Rule for sessions: never answer your own ask. `answer.by.via` says which path an answer came by: `elaia` = the Throne passed a tailnet login and address (`--user` and `--ip`, both; the Throne refuses a tap that has no login); `ssh` = a shell over ssh, which can't claim the Throne's path because `--user` and `--ip` are refused in an ssh session, as `zeus approve` refuses them; `local` = a shell on `{{SERVER}}` itself. What it can't do: a local shell on `{{SERVER}}` running as the owner's user can still pass both flags, and nothing (no Janus) checks them for an answer, because an answer runs nothing. So `via` is a record of the path, not a proof of who tapped; the proof for anything that runs stays Janus's (§4).

### The decision inbox (tier 3), as built in 0.5.0
Until the owner reads the inbox on the Throne, sessions keep asking him in their own question rounds; once he confirms he does, the hub gets a proposal to point sessions here instead (the owner's call, not this repo's).

**The record**, `~/.local/share/zeus/queue/ask-<YYYYMMDD-HHMMSS>-<project>[-n].json` (the id is the file name; asks stay in `queue/` in every state, and the deploy queue skips `ask-*` files):
```json
{ "id": "ask-20261109-090000-zeus", "kind": "decision", "tier": 3, "state": "open",
  "project": "zeus", "title": "Tiers as proposed?", "options": ["Yes, as proposed", "Tier 1 without the second tap"],
  "context": "A few lines the owner reads first.", "asked": "2026-11-09T09:00:00-07:00", "expires": "2026-11-12T09:00:00-07:00",
  "by": { "user": "<shell user>", "via": "ssh" }, "answer": null, "zeus": "0.5.0", "sig": "<hmac-sha256 hex>" }
```
- **States:** `open` → `answered` (once) | `expired`. `answer` becomes `{ "option": "…", "index": 2, "by": { "user", "via", "ip"? }, "at": "…" }`.
- **Signed:** `sig` covers the whole record without `sig` (the same canonical JSON as §4), re-signed by Zeus at each change of state. Every change also appends a signed line to `log/approvals-YYYY-MM.jsonl`: `ask` (title, options, expiry, who asked), `answer` (option, index, who answered), `ask-expired`; each with `"kind": "decision", "tier": 3`. `zeus verify-log` checks the lines and, by default, the ask records in `queue/` too (or one record, or a folder of them).
- **Once:** the first answer makes `queue/.answers/<id>.json` with `O_EXCL`, so two answers at the same moment can't both win; a second answer is refused with the first one named. A record whose `sig` no longer matches is refused, never answered, and `zeus asks` leaves it out (naming it on stderr). Without the key (`secret.env` missing) `zeus asks` says so in plain words and exits 1, never a traceback.
- **Expiry:** enforced when answering and when `zeus asks` lists (no timer); the page shows an open ask past its expiry as expired without writing.
- **Limits:** `project` = lowercase letters, digits and dashes (up to 40); `title` one line, up to 200 characters; 2 to 8 options, one line each, up to 80, all different; `context` up to 2000 characters (new lines allowed); `--expires` 10m to 30d.

**The command line** (one-shot, no daemon, nothing started):
```
zeus ask --project <id> --title "…" --option "a" --option "b" [--context "…"] [--expires 3d] [--json]   # prints the id
zeus answer <id> <option text>   |   zeus answer <id> --index <n>                                      # once
zeus asks [<id>] [--open | --all] [--project <id>] [--json]                                            # sessions read answers here
```
Exit codes: 0 done · 1 refused (unknown, not open, already answered, expired, changed outside Zeus, no key) · 2 bad input.

**The Throne:** **Needs your answer** sits above **Waiting for your yes** in the left column. One card per open ask, the one waiting longest on top: the question, a state chip (? needs your answer), who asked (the project id), when, the expiry, the context as text, and the options as buttons in a group labelled with the question. A tap (pointer, Enter or Space) sends `POST api/throne/answer { id, index }`; the page says "Answered …" in the status line, and the card leaves. With motion allowed the card glints once (`api.fx`); with reduced motion nothing moves. Empty state: "Nothing is waiting for you." Below it, a closed **Answered and expired** list (the last ten) shows each answer and who gave it. Everything a session wrote is escaped text, never markup.

**`GET api/throne/summary`** (one small JSON object; for Elaia's Home or any page that only needs counts):
```json
{ "asks": 2, "deploys": 1, "needs_you": 3, "line": "2 questions for you · 1 deploy waiting" }
```
`asks` = open asks, `deploys` = requests waiting for a yes (`pending`), `needs_you` = both. Home's own `api/throne/status` counts asks too: "Answer 1 question", "Answer 2, review 1", or the deploy wording as before.

## 15. Staged packs
**Status: built in 0.6.0** (designed 2026-11-09 from the game session's request of 2026-11-04, merged the same day). Nothing uses it until the owner gives a repository `kind` `staged-pack` in `repos.json`. It changes nothing for the repositories of §2: they keep `kind` `git` (the default) and everything above.

**Why.** Some repositories cannot be built on `{{SERVER}}` from git. A game's build needs models, sounds and art that live only on `{{PC}}` (outside git, by the owner's rule), and its repository is private with no build tools on the server. Until now its deploy was a line the owner ran on `{{PC}}`. The owner wants to approve it on the Throne instead, from `{{PHONE}}`, while still never letting Zeus run a command from a message. So the session on `{{PC}}` builds and **stages an immutable pack** on the server, then asks; Zeus checks the pack and the commit **himself**, at the request, at the hold and again before the install; the owner's 3-second hold is the yes; Zeus runs **one fixed install script** on **his own copy** of the pack; a rollback puts back the **previous set-aside copy**. Zeus builds nothing and never deploys a pack that is not from a request.

### 15.1 `repos.json`: kind `staged-pack`
```json
"you/game": { "kind": "staged-pack",
              "url": "git@github-game-zeus:you/game.git", "private": true, "key": "~/.config/zeus/keys/game",
              "staging": "~/.config/argus-ui/.staged-game",
              "install": "~/.config/zeus/install/game.sh",
              "health": { "service": "argus-ui" }, "services": ["argus-ui"] }
```
| Field | Meaning |
|---|---|
| `kind` | `git` (default: §2, a clone and the commit's `scripts/deploy.sh`) or `staged-pack`. Anything else fails `check-config` |
| `url`, `private`, `key` | As §2. Zeus still fetches `main` himself, into his bare mirror under `mirrors/` (there is no clone, so `path` fails `check-config` for this kind) |
| `staging` | **The fixed staging folder**, absolute or `~/…`. `check-config`: a folder, not a symbolic link, owned by Zeus's user, not writable by group or others. Missing is a warning (make it before the first request) |
| `install` | **The fixed install script**, absolute or `~/…`. `check-config`: a regular file, not a symbolic link, owned by Zeus's user, and neither it nor its folder writable by group or others; it may not sit inside `staging` (whoever stages must not be able to change what installs). Missing is a warning; Zeus refuses to deploy until it is there. Checked again at every run |
| `health`, `services` | As §2: the same fixed list (`http` on 127.0.0.1, `service`, `exec` from the three agents). An `http` answer with a `version` must equal the pack's, so Elaia's own `api/health` (which says Elaia's version) is the wrong check for a plugin; `service` is the usual one |
| `rules` | As §2; the diff comes from the mirror |
`staging` and `install` are refused on a `git` repository. **Not in config, on purpose:** the pack's file name (Zeus builds it from the version and the commit, §15.3), the arguments the script gets and the order of the checks (fixed in code).

**Why a config may name a script here, when §0 says it never does.** For a `git` repository the script comes from the approved commit, so naming another one in config would let a file widen Zeus. For a staged pack nothing inside the pack runs: the one script is a file the owner installs on the server once, `check-config` makes sure only Zeus's own user can change it (the same user who could edit `repos.json`), and it is never inside the folder sessions write to. The repository's `CLAUDE.md` rule "no config field names a script" needs a one-line exception for this kind; that is the owner's edit.

### 15.2 Staging (the session's part)
1. **Build** on `{{PC}}` from `main` at the commit (`git switch main && git pull --ff-only`, build). The pack is the built folder as a gzip tar with `VERSION` at its top, e.g. `tar -czf <version>-<sha>.tgz -C dist .` (the game's `dist/` has `VERSION` and `assets.json`).
2. **Upload under a temporary name, then rename** in the staging folder: `scp` to `<staging>/.incoming-<version>-<sha>.tgz`, then `mv` it to `<staging>/<version>-<sha>.tgz` and `chmod a-w` it. A half-copied pack never carries its final name.
3. **Write the checksum** `<staging>/<version>-<sha>.tgz.sha256`: the output of `sha256sum <version>-<sha>.tgz`, or the 64 hex characters alone.
4. **Ask:** `ssh {{SSH}} 'zeus stage-request you/game --version <version> --sha <40 hex> --tests "<one line>"'`.

| File in `<staging>/` | What |
|---|---|
| `<version>-<sha>.tgz` | the pack; `sha` is the full 40-hex commit on `main` it was built from |
| `<version>-<sha>.tgz.sha256` | its sha256 (`<hex>` or `<hex>  <version>-<sha>.tgz`) |
A staged pack is **immutable**: never overwritten. To stage the same commit again, set the old pack aside first (a `.set-aside-<date-time>-` name) and ask again; a request whose pack changed is refused at the hold.

### 15.3 The request: `zeus stage-request`
```
zeus stage-request <repo> --version <version> --sha <40 hex> [--tests "<one line>"]
```
**The request carries `{repo, version, sha}` and nothing else Zeus acts on.** `tests` is a note for the card ("412 passed"), shown as text, up to 200 characters on one line; Zeus acts on nothing in it. There is no path, file name or command in a request: Zeus checks `version` against `^[0-9]{1,4}.[0-9]{1,4}.[0-9]{1,4}(-[0-9a-z]{1,16})?$` (dots literal) and `sha` against 40 lowercase hex **before anything is read** (exit 2 otherwise), then builds the name himself: `<staging from repos.json>/<version>-<sha>.tgz`. A version cannot hold `/`, `..` or anything else that would leave the folder.

Then, in order (every refusal is a `refused` record with the reason, as §3; exit 1):
1. the repository is in `repos.json` with kind `staged-pack`. A Hermes deploy-request message, `zeus request` (the Throne's Re-check) and `zeus deploy` are refused for such a repository: its requests come from a staged pack only. A request for the same repository, commit and version already waiting is refused as a duplicate;
2. the pack and its `.sha256` are regular files directly in the staging folder (not links), owned by Zeus's user and not writable by group or others; the pack is at most 2 GB and its modification time is not later than now;
3. the sha256 of the pack equals the `.sha256` file;
4. the pack is a gzip tar of plain files and folders only (no absolute names, no `..`, no links, devices or pipes; at most 100 000 entries and 8 GB unpacked) with a `VERSION` file at its top equal to `version`;
5. the pack's size and modification time did not change while Zeus read it;
6. Zeus's own fetch of `main`: the commit is on `origin/main`, and its `VERSION` file equals `version`;
7. subject, changelog (the first section of the commit's `CHANGELOG.md`) and the files changed since what is deployed, from the mirror.

The record is §3's with `"kind": "staged-pack"`, `"tests"` and
```json
"pack": { "name": "0.46.0-<sha>.tgz", "size": 44145823, "mtime_ns": 1762700000123456789, "sha256": "<64 hex>",
          "files": 712, "unpacked": 101234567, "checked_ns": 1762700040000000000 }
```
`size` and `mtime_ns` are the pack as Zeus read it; `checked_ns` is the clock when he did. Superseding (a newer request for the repository), expiry, Decline and History are as §3. Exit codes: 0 pending · 1 refused · 2 bad input (nothing read).

### 15.4 The hold, and the run
**The card** (Waiting for your yes): the version, the commit (link), its subject and changelog, "Tests: 412 passed" as the session reported it (or "no tests reported"), "Pack 42.1 MB · 712 files", the files changed and the expiry. Buttons: **Deploy** (hold) and **Decline**. There is no "Check main again" for this kind: Zeus can't make a pack; the session stages a new one.

**The hold** (`zeus approve`, §4) checks, before Janus is asked:
1. the record is `pending`, fresh, and the commit is the one the page showed (§4);
2. a fresh fetch: the commit is still an ancestor of `origin/main`;
3. the pack: still a plain file under the same name, the **same size and modification time** as recorded (else "replaced or changed after the request"), not **newer than the request** (`mtime_ns` ≤ `checked_ns`), the `.sha256` still says the recorded hash, and the **sha256 recomputed now** equals both.
Any failure ends the record `refused` with the reason on the card; nothing ran, nothing is retried, and a new `zeus stage-request` is needed. Janus is asked as §4 (`action: deploy`); the signed line gains `"kind": "staged-pack"` and `"pack": { "sha256", "size" }`.

**`zeus deploy-run`** (detached, one at a time, §5):
1. the record's approval is a signed line in the log, with the same pack sha256;
2. the install script passes the `check-config` rules again;
3. fetch `main` again; the commit is still on it;
4. **Zeus copies the pack into his own folder**, `~/.local/share/zeus/packs/<id>.tgz` (mode 600), hashing as he copies, checks that hash against the record, and checks the tar rules and `VERSION` **on his copy**. From here on nothing reads the staging folder: a pack swapped after this point cannot reach the install;
5. `bash <install> install <copy> <version> <sha>`, in the packs folder, with `ZEUS_ID`, `ZEUS_SHA`, `ZEUS_VERSION`, `ZEUS_PREVIOUS` and `ZEUS_PREVIOUS_VERSION` (what runs now, or empty), `ZEUS_PACK_FILES` (the files Zeus counted in the pack) and `ZEUS_PACK_SHA256`; output into the run log; `deploy_timeout_s`. The script prints `ZEUS_STAGE=install` before its first change;
6. the health check from the fixed list; an `http` answer with a `version` must equal the pack's;
7. `state/<repo>.json` as §5; the entry carries the pack's sha256. Zeus's copy stays in `packs/` (never deleted; a cleaning job may trim old ones after a preview, like `.done/`).

**When it fails** (§6, unchanged): before `ZEUS_STAGE=install` nothing changed and Zeus holds; after it, a timeout or a failed health check makes Zeus run `bash <install> rollback <live version> <live sha>` **once**, then the health check. Zeus never retries on his own.

### 15.5 The install script
[`examples/install-pack.sh.example`](../examples/install-pack.sh.example), the install half of the game's old deploy line from `{{PC}}`. Copy it to the `install` path, set `NAME` (and `CHECK_FILE`) at its top, `chmod 755`. It never deletes anything.
- **`install <pack> <version> <sha>`:** unpack into a new `.incoming-<date-time>-<name>` folder beside the live one; check `VERSION` = version, the number of files = `ZEUS_PACK_FILES`, and one named file's bytes (looked up in `assets.json` when its name is content-hashed) = its size in the pack; print `ZEUS_STAGE=install`; move the live copy into `.set-aside-<date-time>-<name>/<name>` with a `zeus-sha` file beside it naming the commit it was; move the checked copy in; check `VERSION` again.
- **`rollback <version> <sha>`:** find the newest `.set-aside-*-<name>` whose copy's `VERSION` is version (and whose `zeus-sha`, when there is one, is sha). None: say so and exit 1 **before** the stage line, so Zeus holds. Else print the stage line, set the live copy aside the same way, move that copy in, check `VERSION`.

### 15.6 Rollback
By hand, as §6: the Throne's **Roll back → 0.45.0** hold or `ssh {{SSH}} 'zeus rollback you/game'`. The target is `state.previous`; Janus verifies who; Zeus writes the signed line; `deploy-run` runs `bash <install> rollback <version> <sha>` and the health check. It needs no pack. Rolling back twice goes one step further back, as long as the set-aside copies are there (they are never deleted). Never retried on its own.

### 15.7 What can a hostile or confused session do?
- **Path traversal in the version string** (`1.0.0/../../x`): refused as bad input before any file is read; the name is built by Zeus from checked parts, and the file must sit directly in the staging folder, not be a link.
- **Swap the pack after the request:** the hold compares size, modification time and a fresh sha256 with what was recorded, and refuses. Between the hold and the run: `deploy-run` hashes its own copy and fails before the install (nothing changed, held). After the copy: the install reads only Zeus's copy.
- **Swap the pack and its `.sha256` together:** the recorded hash no longer matches; refused.
- **A pack for a commit not on `main`, or a `VERSION` that differs** (request, commit, pack): refused.
- **A pack with a link, an absolute name or `..` inside:** refused at the request, and again on Zeus's copy before the install.
- **Make Zeus run something from the request:** no field of a request is a path, a file name or a command, and `tests` is shown as text.
- **Change the install script:** only as Zeus's own user. That account can already edit `repos.json` or run anything, so these checks protect against mistakes, other accounts and a session that writes only where it stages; not against the owner's own account (the same limit as §14).
- **Flood requests:** each is a record; a newer one supersedes the older; nothing runs without the owner's hold.

**Once, by the owner:** a read-only deploy key for the game's repository (Zeus's own, like §2's private repos) and its ssh alias; the staging folder (`mkdir -p` it, `chmod 700`); the install script copied from the example; the `repos.json` entry through the private config repo; `zeus check-config`.

## 16. Root actions from the phone
**Status: designed 2026-11-11, nothing built.** This section is the public, condensed form of a longer private design (v3, with the threat model's evidence, every screen's words and the review that shaped it); a slice is built from the two together. Nothing under `root/` exists yet. The first slice (Z1) is this text and `docs/contract.md` §8. The record formats are in `docs/contract.md` §8.

### 16.1 What it is for
Some changes on `{{SERVER}}` need root: a firewall rule, a service restart. Today the owner types a `sudo` line at `{{PC}}`. This adds a second way: **one listed change at a time, approved from `{{PHONE}}` with the fingerprint, for exactly that change, just now.** It adds a way and never replaces `sudo`. The list is fixed and short; it is never a free command.

### 16.2 Threat model, in short
What is protected is root on `{{SERVER}}`. The owner's yardstick: a design that lets "whoever counts as the owner" cause a root action without his fingerprint is not acceptable. The facts that shape the answer:
- Everything Serve answers on port 443 is one origin (Elaia, its plugins, other pages), and every login that reaches Elaia counts as the owner. So a yes given **inside Elaia** proves nothing to root.
- A passkey's RP ID ignores the port, but the origin written into the signed client data does not. The key page therefore lives on **its own HTTPS port** of the same address, and root checks the exact origin.
- Anything running as the normal user on `{{SERVER}}` (a plugin, a downloaded tool, a session) can rewrite Elaia and Zeus. So nothing it can write may start a root action, and nothing it can write may change what the owner reads when he taps.
- A pull request that merges itself is not a pull request the owner read. Root code is therefore the exception to auto-merge (`CLAUDE.md`).
- The screen PIN may stand in for the fingerprint. Until a test away from home shows otherwise, the list holds only changes that have an undo, plus restarts, which change no setting.

Attackers considered: another tailnet login, a script injected on port 443, any process as the normal user, a stolen unlocked phone, a replayed or altered request, a session that asks for one thing while the owner approves another, a bug in the root side, a taken-over Google account, a bad action script, and a Claude session or browser extension on `{{PC}}`. For each, the private design states what it must never manage.

### 16.3 The pieces
| Piece | Runs as | What it does |
|---|---|---|
| **Key page** (static files, one `list.json`, a byte-identical copy of the installed `actions.json`, so the page and root hash the same bytes) | nobody: served by Tailscale Serve from a root-owned folder | shows what will change and how to undo it, asks the phone for the signature, shows progress and the last ten runs |
| **`zeus-root-gate`** | its own system account: no login, no home, no rights; socket-activated on `127.0.0.1:<gateport>` (below 1024, so only systemd can bind it) | passes messages: checks they come from the key page, from an owner, for a nonce it issued; writes one record into root's inbox. It never decides whether a signature is good and never runs anything |
| **`zeus-root`** (the runner and its command) | root, a one-shot started by a path unit, **no network** | takes records out of the inbox, verifies the signature and everything around it, runs one listed script, undoes once on failure, keeps the reach check |
| **`actions.json`** (the fixed list) | root-owned file | per entry: id, kind, words, script, its sha256, undo, flags. Changes only by the owner's own line at `{{PC}}` |
| **Zeus side** (later slices) | the normal user | `zeus root-ask <id>` makes a tier R ask; the Throne shows a card; History reads root's log |

No polkit rule and no sudoers line: the normal user gains no new right. Root holds public keys only. Idle cost is zero (a socket unit and a path unit are kernel watches, like `zeus-queue.path`). The installer checks that the unprivileged-port floor really is 1024 before it relies on `<gateport>` being below it. The contract is `docs/contract.md` §8; the gate's rules and the runner's checks are written out in 16.4, and the private design (v3) holds the rest of the evidence.

**Who may read what.** A normal-user process must never be able to read a key id, a user handle or the public keys' file. Root applies these rules before every run, and a run is refused if one is wrong:
| Path | Owner, mode | Holds |
|---|---|---|
| `/etc/zeus-root/config.json`, `actions.json` | root 644 | the exact origin, ports, game list; the fixed list |
| `/etc/zeus-root/keys.json` | root:`zeus-root-gate` 640 | registered keys: ids, user handles, public keys, device, claim |
| `/etc/zeus-root/owners.json` | root:`zeus-root-gate` 640 | the logins allowed to use the gate |
| `/etc/zeus-root/labels.json` | root 644 | **no ids**: key ref, label, device name, action title, dates; Elaia reads this one |
| inbox and `enrol/` | `zeus-root-gate` 700 | records waiting for root |
| `work/`, `quarantine/`, `/var/lib/zeus-root/` | root 700 | records taken out; entries that were not records; nonces, counters, the pause switch |
| `results/`, `/var/log/zeus-root/` | root 755, files 644 | results, run logs, `runs-*.jsonl`, `attention.jsonl`: no key id, user handle or secret |
| `/var/lib/zeus-root-gate/` | `zeus-root-gate` 700 | the gate's issued nonces and rate counts |
Every script sits in `actions/` and nowhere else; nothing above is a link; nothing else is writable by group or others.

### 16.4 The flow, from ask to run
1. A session runs `zeus root-ask <id>`. Zeus makes a tier R ask whose title is copied from the list; the phone buzzes *Needs your yes*. The ask can make nothing run.
2. The owner opens the key page (from Elaia's card by a plain `noopener` link, or from a home-screen icon that never passes through Elaia). The page reads the action once, refuses to run framed or while it has an opener, blanks itself if its address changes, and keeps its button inactive until its words have been on screen for about a second, and inactive again (another second) whenever the page was hidden or lost focus; a press that began earlier does not count. A `<meta>` Content-Security-Policy (`default-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'`) makes the browser refuse code or connections from anywhere else.
3. He taps the button named after the change (for example "Close the 3 ports"), then touches the fingerprint sensor. Four acts: the buzz, Review and confirm, the button, the fingerprint.
4. **The challenge** is SHA-256 over `zeus-root/v1\n` and the canonical JSON of the action id, its params, the sha256 of the installed `actions.json`, a random nonce, an expiry (at most 180 s) and the key page's exact origin. The page recomputes it from its own root-owned copy and refuses on any difference, so a lying gate is caught.
5. **The gate checks, in this order** (a request that fails any step gets one short JSON refusal and is counted apart from the owner's tries):
   1. The other end of the TCP connection belongs to uid 0 (Serve), read from `/proc/net/tcp`.
   2. Only `POST`, to one of six paths (`begin`, `finish`, `result`, `reach`, `history`, `enrol`); any other method, `OPTIONS` included, gets 405. A `Content-Length` of at most 16 KB is required; a chunked body is refused.
   3. The exact `Origin` of the key page; `Sec-Fetch-Site` is `same-origin` when present; `Content-Type` is `application/json`. Port 443, any other port and any form or plain-text POST are refused before the body is read.
   4. No answer carries an `Access-Control-*` header; every answer carries `Content-Type: application/json`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.
   5. The body is one JSON object with exactly the keys that endpoint expects: no repeated and no extra key.
   6. Exactly one non-empty login header (a tagged device or Funnel has none, so it is refused) and exactly one `X-Forwarded-For` holding exactly one address that is not the server's own. **A missing `X-Forwarded-For` is refused; the address range never decides** (Elaia's own-machine check lets a missing header through, so it is not copied as is). `tailscale whois` on that address must name a device whose user equals the login.
   7. **The owner check, on every endpoint** (`begin`, `finish`, `reach`, `result`, `history`, and `enrol` after the first sitting): the login must be in `owners.json`. The one exception is `enrol` during the very first sitting, while `owners.json` is still empty; the pairing code and the owner's typed answers at `{{PC}}` (16.11) stand in.
   8. Per-login and per-device limits, checked after the owner check so other logins never use up the owner's tries.

   The endpoints add: `begin` is refused for an unknown id and while a game runs, stores the nonce as *issued to this login, this device and this action*, and offers only that action's keys registered for the asking device. `finish` and `reach` are accepted only for a nonce this gate issued to the same login, device and action, unexpired and not finished before, and the `credentialId` must be one offered for that nonce; a repeat gets the same run id and writes nothing. `result` and `reach` answer only for a run approved from this login and device. `enrol` works only while root has a window open (the flag file is root's), at most 5 tries per device and 15 in all, then the window closes. The gate then writes one record. It never runs anything, never holds a key id in its log or the journal, and never decides whether a signature is good.
6. **The runner**, for each start: renames every inbox entry into a root-only folder first; does this for at most 50 entries per start, and more than 50 waiting or more than 200 in quarantine sets the pause and writes an attention line (a flood means a broken or hostile gate); opens each with `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` (without `O_NONBLOCK` a FIFO would block the runner for ever and stop every root action) and checks the open file (a regular file, owned by the gate, one link, at most 16 KB), reading from the handle, never the name again; parses strictly (exactly the expected keys and types, no repeated keys); anything else goes to quarantine, so every entry leaves the inbox. A record left in `work/` by a crashed earlier start goes to quarantine and is never re-run. Then it checks the file rules, the pause switch, the lock, the limits and **Serve's mapping** (the key page's port still maps to the page folder and the gate; no Tailscale operator is set). It verifies the assertion: the key is registered, not revoked, belongs to that action and that device; client data type, challenge, exact origin, not framed; the RP ID hash; user present and user verified; the counter; the signature; freshness; the list and the script's hash. The counter rule: when the stored or the new sign counter is not zero and the new one is not larger, refuse and alert; zero and zero is allowed, because synced passkeys keep it at zero (so neither "must always increase" nor "only a decrease is wrong" is the rule). It marks the nonce used **before** anything runs, refuses while a game runs (for an entry with `game_check`), then runs the script with no arguments, working folder `/`, stdin closed, and an environment of exactly two variables, `PATH=/usr/sbin:/usr/bin` and `ZEUS_ROOT_RUN=<run id>`, for 120 s at most (the runner's own limit is 180 s).
7. **Hold before the change, roll back after it** (the rule of §6). Any non-zero exit before the script printed `ZEUS_STAGE=change` means stopped, nothing changed. After it, the entry's undo runs once. An undo that fails too writes an attention line. An entry with no undo (a restart) has nothing to run; the result says the service did not come back, and the attention line buzzes.
8. **The reach check** for an entry that can cut the phone off: root writes a deadline (10 minutes) to disk and starts a template timer. The key page, polling `result`, sends a `reach` record: kept. Any opening of the key page from that device checks in. At minute 5 without a check-in one reminder buzz. At the deadline, with no check-in, the paired undo runs by itself, also after a restart (a boot unit handles passed deadlines), and one *Something stopped working* buzz follows. A running game delays the put-back by at most 15 minutes.
9. The result and one log line are written; the page says Done. "Still answers this phone" is all the check-in proves.

### 16.5 What is refused
A request that is not a POST from the key page's own origin; a login that is not an owner; an unknown or revoked key, a key for another action or device; a framed page; user verification missing; a stale, used or expired nonce; a changed list or script; a counter that is not larger when either counter is not zero (16.4 step 6); Serve's mapping changed, or a Tailscale operator set; files that break the ownership and mode rules; a game running (there is no "do it anyway"); another run holding the lock (30 s, then busy); a paused system; a limit reached. Three failed signatures of a registered key in an hour pause every root action for an hour. Unknown or malformed records are "garbage": logged, counted apart, never a pause. The log names the class of every refusal.

### 16.6 The first list
| id | Kind | What | Undo | Game check | Can cut you off |
|---|---|---|---|---|---|
| `self-test` | check | runs as root and reports versions, file rules, Serve's mapping and the keys it expects; changes nothing | none needed | no | no |
| `close-media-lan` | change | closes the three media ports to the apartment network, still open through Tailscale | `open-media-lan` | yes | yes |
| `open-media-lan` | put-back | opens them again | `close-media-lan` | yes | yes |
| `restart-bluetooth` | change (a restart) | restarts `bluetooth.service`, waits up to 15 s until it is active | none (`"undo": null`) | yes | no |
| `restart-resolved` | change (a restart) | restarts `systemd-resolved.service`, waits up to 15 s until it is active | none (`"undo": null`) | yes | no |

A restart changes no setting; the service starts again from its saved settings, so there is nothing to put back. An entry with `"undo": null` must have `can_cut_you_off: false`. `check-key` (not an entry) lets the Self-test try every registered key and runs nothing. `tailscaled`, `NetworkManager` and `firewalld` stay off the list because a restart can cut the phone off and has no undo to run; `sddm` ends the Game mode session and belongs to tier 2. An entry is added only in four steps: a session writes it, a fresh-context reviewer reads it hostilely, the owner installs it with his own line at `{{PC}}` (the installer prints every entry added, changed or removed, with checksums), and he registers its key in the same sitting. Never from a page, a message, a session or a push.

### 16.7 The owner's decisions (2026-11-11)
- **Build it, in small steps.** The root parts never merge by themselves; each slice gets a hostile fresh reviewer and the owner's yes before it merges. The first list also holds service restarts: `restart-bluetooth` and `restart-resolved`.
- **The gesture:** one tap on the button named after the change, then the fingerprint; the button becomes active after about a second.
- **A game running** at the start means "try after the game". A game running when a put-back is due delays it by at most 15 minutes.
- **No spare key.** The `sudo` line at `{{PC}}` is the spare.
- **Kept as proposed:** one passkey per action, saved in the phone's password manager and tied to the phone's tailnet device (the name on Android's sheet is a cue, not a lock); setup only from an address the terminal shows (typed; no QR code), never from Elaia; a home-screen icon; asks wait 24 h for network changes and 1 h for restarts; the challenge lives 180 s; no buzz for "done"; Decline needs no fingerprint (owner only, once a people file exists), and declining or letting an ask expire does not disable the action: the ask is a nudge, root ignores it, and nothing in the runner may check the ask; the list changes only from `{{PC}}`; release installs are not on it; names `zeus-root`, `zeus-root-gate` and "the key page", under Zeus.
- **Tailscale's operator is removed before the install.** Today the normal user is Tailscale's operator (so Hermes can use Taildrop without `sudo`), and an operator can repoint Serve. A program running as that user could then put its own page at the key page's exact origin and show one change while getting a signature for another, or slip in its own key at setup; the runner's check looks at one moment and can be stepped around. So the operator is removed first, Hermes gets a Taildrop path that does not need it, and the installer refuses while an operator is set. Changing Serve then needs `sudo`, as every setup line already does.

### 16.8 Build slices
Every slice that touches `root/` follows the rule in `CLAUDE.md`: no auto-merge, a fresh-context hostile reviewer, the owner's yes in the chat.
| Slice | What | Tests (the main ones) |
|---|---|---|
| **Z1** | this section, tier R in §14, `docs/contract.md` §8 | `check-private` |
| **Z2** runner and command | the checks of 16.4 step 6 and 7, the reach tick and boot check, `check-key`, `enroll` (the two typed prompts of 16.11, DER keys, no CBOR), `rebind`, `revoke`, `keys`, `verify-log`, `pause` | the WebAuthn test vectors; real and forged assertions; a hostile inbox (link, FIFO, folder, socket, hard link, swapped file, 60 entries); pause counting; undo once; reach cases; `"undo": null` rules |
| **Z3** gate and units | the gate's rules one by one; sysusers and tmpfiles files; the units | each refusal; nonce bound to login and device; finish once; no `Access-Control-*` header; no key id in any log |
| **Z4** key page | the screens, the challenge recompute, the frame, opener, hash-change and one-second rules, Wake Lock | a browser with a virtual authenticator, at 360 px, 44 px targets, keyboard, reduced motion |
| **Z5** install bundle | installer, uninstaller, `actions.example.json`, script examples | a dry run in a container; set aside, never delete; refuses while an operator is set |
| **Z6** Zeus side | `zeus root-ask`, the Throne's Root changes section, History rows from root's log | ask expiry per entry; unknown id refused; every outcome parsed |
| **A1 to A3** (argus-ui) | the needs-yes path for tier R with a plain `noopener` link; a page that explains the setup and lists keys as labels (no setup link, no ids); buzzes from `attention.jsonl`, the minute-5 reminder, summary counts | their own repo's tests |

Order: Z1; Z2 and Z3; Z4; Z5; one hostile review of Z2 to Z5 together; the owner's install (after the operator is removed); Z6 and A1 to A3, which only read.

### 16.9 What stays private, and what stays unsolved
**Never in this repository:** the real `actions.json`, the real action scripts, the installer with the real address, ports and checksums, the tailnet name, the owner's names. The repository carries `.example` files with placeholders.

**Unsolved or limited:** the PIN may stand in for the fingerprint (to be tested away from home); passkeys of the key page share a host name with port 443 until a name of its own exists (a later version), which could let a page on 443 delete them silently or learn their ids, though no root action can come of it; the name on Android's sheet is a cue; a bug in the root side is still a root bug; a taken-over tailnet admin gets root by an SSH rule without this design; expiry trusts the server's clock; "gaming first" is only as good as the game list; Elaia's buzzes can be silenced by a program as the normal user, so the key page's own last-runs list, read from root's log, is the place that cannot lie.

### 16.10 Recovery
Revoking marks a key revoked and never deletes it, so `verify-log` can still find it by `key.ref` and the log stays checkable.
| What happened | What to do |
|---|---|
| Phone lost or stolen | first remove it from the tailnet's machine list (the key page is then unreachable from it); then `revoke` for that device at `{{PC}}`; then remove it from the Google account. Revoking at the PC is what stops the keys, deleting passkeys alone does not |
| New phone | the first registration (16.11) again, at home |
| Tailscale app reinstalled or the phone re-added (a new device id, so every key is refused by the device check) | `rebind` at `{{PC}}`: it shows the old and the new device, the login and the keys, and asks for a typed yes; or register again |
| Passkeys missing on the phone | the Self-test's check of every key shows which; register again and revoke the old ones |
| Every key lost | the `sudo` line at `{{PC}}` is untouched; then `enroll` |
| Google account taken over | Google's recovery first; then revoke the phone's keys and register again. The per-device check refuses copies on new devices meanwhile |
| Tailnet name changes | the RP ID changes, so every key must be registered again |
| Paused (3 failed signatures of registered keys in an hour, or a flood) | everything pauses for an hour; `resume` at `{{PC}}` ends it sooner |
| The watcher unit failed | a buzz; it returns at the next boot, or restart it with `sudo` |
The key page adds a way and never replaces `sudo`.

### 16.11 First registration (at home, once)
A registration carries no maker's signature, so a key made by a script would look like the phone's. These things together lock the sitting:
1. At `{{PC}}`, root opens a 10-minute window and prints the address to type on the phone (no QR code, never a link from Elaia) and a 6-digit pairing code. Elaia's page about setup says "start at `{{PC}}`" and has no link.
2. On the phone, in Chrome itself, the owner checks that the address bar shows the key page's port, types the pairing code and a label, and touches the sensor once per action (one passkey per action, no attestation, public key and flags taken by position, no CBOR). The gate takes setup requests only from the key page's own origin; 5 tries per device and 15 in all, then the window closes.
3. The page shows five check words made from every key of the sitting, plus the phone's own claim of where it saved them, worded as the phone's claim that the server cannot check.
4. At `{{PC}}`, in the same terminal, **two typed questions, answered by the owner and never by a session**. First, device, login, **number of keys** and the action names, with the phone's claim: "is this your phone, your login and these keys?". Second, the check words: "does the phone show exactly these words?". Only after two yeses does root write `keys.json`, `owners.json` and `labels.json`; a no or a different word writes nothing. A program as the normal user could print a fake block into the terminal, but it cannot register anything: root registers only what the genuine key page submitted with the pairing code, and reads the yes from its own prompt.
5. Still in the sitting: add the home-screen shortcut (Chrome menu, Add to Home screen) and run the Self-test and the check of every key (one touch each).
6. Later, the first time away from home: the PIN test (16.9, "unsolved").

## References
- Akhawe, D., & Felt, A. P. (2013). Alice in Warningland: A large-scale field study of browser security warning effectiveness. *USENIX Security '13*.
- Nielsen, J. (1994). Enhancing the explanatory power of usability heuristics. *CHI '94*.
- Saltzer, J. H., & Schroeder, M. D. (1975). The protection of information in computer systems. *Proc. IEEE, 63*(9). (Least privilege: fixed branch and script; separation of privilege: Janus decides who, Zeus decides what, Mnemosyne keeps the record.)
- Humble, J., & Farley, D. (2010). *Continuous Delivery*. Addison-Wesley. (Deploy the same immutable artifact; roll back by redeploying a known-good version.)
- W3C (2018). *WCAG 2.1*: SC 2.5.1 Pointer Gestures, 2.5.5 Target Size, 2.3.3 Animation from Interactions.
- Parasuraman, R., Sheridan, T. B., & Wickens, C. D. (2000). A model for types and levels of human interaction with automation. *IEEE Trans. SMC-A, 30*(3). (§14: the tiers scale the owner's involvement with what an action can change; a decision is advice to a session, not an action.)
