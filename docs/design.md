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
| Idle cost | Zero: `zeus-queue.path` (watches `requests/`) and one-shots started by the page or ssh. Zeus has no timer at all | Resource rule |

## 1. What Zeus is, and isn't
- **One job: run what the owner approved.** Zeus turns an approved request into a checkout of exactly that commit and a run of that repo's `scripts/deploy.sh`, records the result, and can put the previous approved commit back.
- **He never fetches from anywhere but the allowlisted repos**, never runs a command from a message, a page or a config, never deploys a branch, never touches a file Hermes carried, never pushes anywhere. The only code Zeus runs that he didn't ship with is `scripts/deploy.sh` of a commit the owner merged and then approved.
- **Nothing of Zeus runs while idle.**
- **Python 3 standard library only**, one zipapp at `~/.local/bin/zeus` (like Hermes and Janus). Calls `git`, `janus`, `hermes`, `systemd-run`, `systemctl --user`, `tailscale`. Subcommands: `queue` (process `requests/`), `request`, `approve`, `approve-release`, `decline`, `deploy-run` (internal, under `systemd-run`), `rollback`, `status`, `check-config`, `init`, `verify-log`.

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
A `server` plugin shipped in `plugin/`, installed to `~/.config/argus-ui/plugins/throne/`, pill "Zeus · Throne", URL `<base>/throne` (`/zeus` is Zeus's task page in Watch; persona keys are reserved ids). Colour: storm gold `#E0B341` on Night. Zeus's bolt medallion is drawn by the page already. Argus small in the corner, glaring while `alerts/zeus.json` exists.

**Page, laid out for a phone first (375 px), list pattern:**
1. **Status line:** "1 deploy waiting · argus-ui 4.8.0 live since Tue". Feeds Elaia's Home page through the plugin `status` endpoint.
2. **Waiting** (the queue, newest first): one card per request: repo, version, sha7 (link to the commit), subject, changelog lines, checks chip (✓ passed · ✕ failed · – none), "12 files", the rules badge when it applies, expiry ("expires in 2 d"). Buttons: **Approve & deploy** (hold), **Decline** (asks for a reason word), **Re-check**.
3. **Deploying:** the live log tail (`aria-live="polite"` for stage changes only, not every line), stage chips (fetch → checkout → build → install → health), Cancel is **not** offered (half a deploy is worse than a whole one; rollback is).
4. **Deployed now:** per repo: version, sha7, when, by whom (user · device), **Roll back → 4.8.0** (hold, same identity check).
5. **History:** everything in `.done/` plus deploys, newest first, with outcome chips (✓ deployed · ↩ rolled back · ✕ failed · ⌛ expired · ↑ superseded · ⊘ declined · ⇥ released), each opening its log; expired and superseded rows carry **Re-check**. Filters: All · Waiting · Expired · Failed.

**Hold to confirm (Approve & deploy, Roll back, Release).** Press and hold for 3 s: a ring around the button fills while the label counts "Hold… 3 · 2 · 1"; letting go early cancels with nothing sent; at 3 s the POST goes and the label turns to "Deploying". Keyboard: hold <kbd>Space</kbd> or <kbd>Enter</kbd> on the focused button for the same 3 s (a key-repeat timer, cancelled on keyup). Reduced motion: no ring, the countdown words only. Screen readers: the button is described by "Hold for three seconds to deploy argus-ui 4.9.0", and the status line announces the result. Touch target ≥ 44 px; the hold never needs a path gesture, so WCAG 2.5.1 (pointer gestures) is met.

**API:** `GET api/throne` and `api/throne/status` read `queue/`, `state/` and the logs directly (no subprocess on reads; `deploy-<id>.log` tail is a file read). POSTs pass Elaia's checks (same-origin, JSON, 4 KB): `approve`, `decline`, `rollback`, `recheck` (starts `hermes-github` then `zeus queue`), `seen`. Every POST that changes what runs calls Zeus, and Zeus calls Janus; the plugin itself never decides.

**Accessibility (argus-ui `CLAUDE.md` + WCAG 2.1 AA):** keyboard path through every card and button with visible focus; no sideways scroll at 375 px, targets ≥ 44 px; state = icon + word, never colour alone; with reduced motion no lightning on approve (a static bolt icon and the word "Deploying"); one `aria-live` region on the status line.

**Why it's built this way.** The request card shows exactly what will run (repo, commit, changes, tests) before the one irreversible gesture, and the result appears where the gesture happened (visibility of system status; Nielsen 1994). Rollback is one hold because the cost of a bad deploy is paid by the same page you'd use to fix it. The hold is the friction, not a dialog: a confirm that repeats the card's text would be clicked through (Akhawe & Felt 2013), while three seconds with the card still in view is time to read it.

**As built in 0.4.0** (`plugin/`, issue #11):
- The page uses argus-ui's shared `holdConfirm` and `fx` (argus-ui 4.18, `docs/fx.md` there): the hold, the bolt on an approval and the crumble on a decline come from Elaia, so the Throne can't drift from the other pages. A click that cannot be held (a screen reader's or a switch's) arms the button and a second click within 5 s confirms.
- Reads change nothing: a waiting request past its expiry is *shown* as expired, and Zeus records it when he next lists the queue or is asked to approve it. `Re-check` is labelled **Check main again** and runs `zeus request <repo>` (Zeus fetches `main` himself); asking `hermes-github` first is left to Hermes's hourly check.
- The rules badge names the changed rules files; the inline diff of those files is not built yet (the record carries only their names).
- The tablet, throne and stage-node art waits for the owner's approval (argus-ui `docs/ui-plan.md` §13); until then the page uses plain cards and chips.
- Tests: `tests/test_throne_plugin.py` (reads, stages, Home's status, the exact `zeus` arguments, refusals) and `tests/throne/throne.test.js` in a browser under Elaia against `tests/throne/fake_zeus.py` (`bash tests/throne/run.sh <argus-ui checkout>`; the `throne-page` CI job).

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
zeus/         cli, config (repos.json), queue, approve (janus call, hmac log), deploy (git, systemd-run, health, rollback), log
plugin/       Elaia server plugin "throne": plugin.json, plugin.py, page script (hold-to-confirm button)
units/        zeus-queue.path/.service, argus-limits drop-in
examples/     repos.example.json (placeholders), deploy.sh.example (what a repo's deploy script must honour: --no-pull, ZEUS_STAGE=install)
docs/         design.md (this doc), contract.md (Hermes → Zeus message, Zeus ↔ Janus, plugin API), deploy-script.md
scripts/      build.sh (zipapp), install.sh, deploy.sh (--no-pull aware), check-private.sh, install-hooks.sh
tests/        run.sh (unittest): a fake git remote (local bare repos), a fake janus, a fake systemd-run; cases for every state transition, expiry, supersede, rollback, hmac verify
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
| private config repo | `zeus/repos.json`, `scripts/deploy.sh`, `mnemosyne-journal` job + write key, branch protection if the plan allows | the owner |

## 13. Build order
| Version | Issues (one branch, one PR each) | Needs |
|---|---|---|
| 0.1 "the queue" | #2 skeleton, config loader, `check-config`, `init`, zipapp build · #3 `zeus-queue`: record, on-main check, supersede, expiry · #4 `request` / `decline` / `status` CLI | Hermes message format (done) |
| 0.2 "the yes" | #5 `approve` (freshness, Janus call, signed log) · #6 ssh approval path (`SSH_CONNECTION`, typed yes) · #7 `verify-log` | janus `verify-approval` |
| 0.3 "the deploy" | #8 `deploy-run` under `systemd-run`: fetch, checkout, `--no-pull`, health · #9 hold vs auto-rollback, `rollback`, `state/` · #10 alert file | argus-ui 4.9.1 |
| 0.4 "the throne" | #11 Throne plugin page (list, hold-to-confirm, live log, History with filters, a11y tests) · #12 `approve-release` + Delivery page button | argus-ui `server` plugin kind; hermes `release --approval` |
| 1.0 | #13 bootstrap on the server, config-repo `deploy.sh`, first approved deploy of argus-ui, retire the manual line | all above; the journal job |

## References
- Akhawe, D., & Felt, A. P. (2013). Alice in Warningland: A large-scale field study of browser security warning effectiveness. *USENIX Security '13*.
- Nielsen, J. (1994). Enhancing the explanatory power of usability heuristics. *CHI '94*.
- Saltzer, J. H., & Schroeder, M. D. (1975). The protection of information in computer systems. *Proc. IEEE, 63*(9). (Least privilege: fixed branch and script; separation of privilege: Janus decides who, Zeus decides what, Mnemosyne keeps the record.)
- Humble, J., & Farley, D. (2010). *Continuous Delivery*. Addison-Wesley. (Deploy the same immutable artifact; roll back by redeploying a known-good version.)
- W3C (2018). *WCAG 2.1*: SC 2.5.1 Pointer Gestures, 2.5.5 Target Size, 2.3.3 Animation from Interactions.
