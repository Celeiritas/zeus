# Zeus – contracts
What crosses Zeus's borders: the message Hermes drops, the record Zeus keeps, the document the Throne page reads, and the signed log line. Times are ISO 8601 with the server's offset. Repositories are `owner/name`; commits are 40 hex characters, shown as 7 on the page. `{{SERVER}}`-style placeholders in examples are filled in from `~/.config/leafy/realm.json` on the server.

## 1. Hermes → Zeus: the deploy-request message
`hermes-github` writes `<name>-<sha7>.json` into `~/.local/share/zeus/requests/` (Hermes route `deploy-request`). Zeus treats it as a hint: he fetches `main` of the allowlisted url himself and checks the commit is on it before anything else.
```json
{ "type": "deploy-request", "repo": "you/argus-ui", "sha": "<40 hex>", "previous": "<40 hex or null>",
  "subject": "Mentor: Learn phase", "version": "4.9.0", "checks": "success",
  "changelog": ["- Learn cards …"], "seen": "2026-10-21T09:00:00-06:00" }
```
| Field | Must be | Else |
|---|---|---|
| `type` | `deploy-request` | refused |
| `repo` | `owner/name`, present in `repos.json` | refused, **nothing is fetched** |
| `sha` | 40 hex | refused |
| `previous` | 40 hex or `null` (absent = null) | refused |
| `subject`, `version`, `checks` | text (optional; `checks` defaults to `none`) | refused |
| `changelog` | list of lines (optional) | refused |
| `seen` | ISO time (optional; now when missing) | — |

Any file that is not valid JSON, not an object, or larger than 64 KB is refused too. **Every file leaves `requests/`**, whatever happens; a refusal is a record in `queue/.done/` with the reason.

`zeus request <repo>` makes the same message itself for the current `main`: `subject` from the commit, `version` from its `VERSION` file, `changelog` from the first section of its `CHANGELOG.md` (10 lines at most), `checks` = `none`, `previous` = what `state/` says is deployed.

## 2. The record (`queue/<id>.json`, `queue/.done/<id>.json`)
`id` = `YYYYMMDD-HHMMSS-<name>-<sha7>` (a `-2`, `-3` suffix when two arrive in one second).
```json
{ "id": "20261021-090012-argus-ui-3fa9c1b", "action": "deploy", "state": "pending",
  "repo": "you/argus-ui", "sha": "<40 hex>", "previous": "<40 hex or null>",
  "subject": "Mentor: Learn phase", "version": "4.9.0", "checks": "success",
  "changelog": ["- Learn cards …"], "files": 12, "rules_changed": [],
  "seen": "2026-10-21T09:00:00-06:00", "expires": "2026-10-24T09:00:00-06:00",
  "verified": { "on_main": true, "at": "2026-10-21T09:00:12-06:00" },
  "approval": null, "deploy": null }
```
- `action`: `deploy`, or `rollback` for a hand rollback (§4a).
- `state`: `pending` or `held` (in `queue/`), then one of `approved`, `deploying`, `deployed`, `failed`, `rolled-back`, `expired`, `superseded`, `declined`, `refused` (in `queue/.done/`, never deleted).
- `reason` (text) is present on every non-pending state that stopped the request: `refused` (why), `superseded` ("replaced by 4.9.1", plus `superseded_by: {id, version, sha}`), `expired`, `declined` (the reason word).
- `files` is the number of paths changed `previous..sha` (the commit's own change when `previous` is unknown; `null` when git could not say); `rules_changed` lists those among the repository's `rules`.
- `approval`: `null` until decided; a decline sets `{ "decision": "declined", "by": {…}, "at": "…", "reason": "<word>" }`; an approval sets `{ "decision": "approved", "by": { "user", "ip", "via", "node" }, "at": "…", "janus": "allowed", "sig": "<the log line's sig>" }`.
- `janus`: after an approval attempt, `{ "verdict": "allowed" | "refused" | "failed", "reasons": [...], "at", "user", "ip" }`. A refusal leaves the record `pending` with the reasons kept for the card; a failure makes it `held` (still in `queue/`, approvable again once Janus answers) and writes the alert file `~/.local/state/argus-ui/alerts/zeus.json` (`{ "message", "link": "throne", "at" }`).
- `deploy`: `null` until approved; then `{ "started": true, "unit": "zeus-deploy-<id>", "at" }` or `{ "started": false, "error": "…", "at" }` when `systemd-run` refused (the approval stands and is logged; the alert file says so). `zeus deploy-run` adds `run_started`, `log` (the run log's path), `finished`, `outcome` (`deployed` | `failed`) and `install_stage` (true once the script printed `ZEUS_STAGE=install`); a failure puts the plain-sentence reason in `reason`.

## 3. `zeus status --json` (what the Throne plugin reads)
```json
{ "zeus": "0.1.4", "at": "2026-10-21T09:05:00-06:00",
  "line": "1 deploy waiting · argus-ui 4.8.0 live",
  "deployed": { "you/argus-ui": { "sha": "…", "version": "4.8.0", "at": "…", "approval": "…" }, "you/hermes": null },
  "queue": [ <pending records, newest first, expiry already applied> ],
  "deploying": [ <records in state approved or deploying> ],
  "history": [ <the last ten terminal records, newest first> ] }
```
- `deployed` has one key per repository in `repos.json`; `null` until Zeus deployed it (`state/<owner>--<name>.json`, written by #8).
- `line` is the one sentence for Elaia's Home page.
- Listing applies expiry: a pending record past `expires` becomes `expired` on the spot (no timer).
- The shape only grows: new keys may appear; existing keys keep their meaning.

## 4a. What is live: `state/<owner>--<name>.json`
Written by `zeus deploy-run` after a deploy passes its health check:
```json
{ "repo": "you/argus-ui",
  "deployed": { "sha": "<40 hex>", "version": "4.9.1", "at": "…", "id": "<request id>", "approval": { "user", "ip", "via", "node" } },
  "previous": { <the entry deployed before it, or null> },
  "history": [ <earlier entries, newest first, 10 at most> ] }
```
`previous` is what a hand rollback puts back.

**When a deploy-run fails** (docs/design.md §6):
| Where | What Zeus does | Request ends |
|---|---|---|
| before the script printed `ZEUS_STAGE=install` (fetch, checkout, build) | nothing else; nothing was changed | `failed`, alert "… failed before the install; nothing was changed" |
| after it, a timeout, or health never passing | puts back what was live before this run (`state.deployed`): its commit, its own script, its health, once | `rolled-back` (alert "… failed and 4.9.0 was put back"), or `failed` with the alert "argus-ui 4.9.1 failed and 4.9.0 could not be restored" (or "… there is no earlier approved commit to put back") |
Zeus never retries on his own. The run log says each step in a sentence (`zeus: … failed after the install had begun: …`, `putting back 4.9.0 (3fa9c1b), the commit that was live before this run, because …`). The record keeps `deploy.install_stage` and, after a restore, `deploy.restored: {sha, version}`.

**A hand rollback** (`zeus rollback <repo>`, from the Throne with `--user --ip --sha`, or over ssh with a typed yes): a record with `"action": "rollback"`, `sha` = `state.previous`, `from` = what runs now, expiring after 10 minutes. Janus verifies with `action: rollback`; a signed `rollback` line goes into the approvals log; the same `deploy-run` does the work, refusing if `previous` changed in between. On success `previous` becomes `deployed` and the stack moves one step: rolling back again goes one step further. Every deploy-run also appends one signed line to `log/deploys-YYYY-MM.jsonl`: `{ "t", "id", "repo", "sha", "version", "outcome", "reason", "install_stage", "zeus", "sig" }`. The run itself is in `log/deploy-<id>.log`: Zeus's steps as `zeus: <time> …` lines, then the script's output. What a repository's script must honour is in `docs/deploy-script.md`.

## 4. The signed log line (`log/approvals-YYYY-MM.jsonl`)
One JSON object per line, keys sorted. `sig` is HMAC-SHA256, hex, over the line's canonical JSON **without `sig`** (keys sorted, `,` and `:` separators, no spaces, UTF-8), keyed by `ZEUS_LOG_KEY` (`~/.config/zeus/secret.env`, mode 600, made by `zeus init`).
```json
{ "t": "2026-10-21T09:05:00-06:00", "id": "20261021-090012-argus-ui-3fa9c1b", "action": "decline",
  "repo": "you/argus-ui", "sha": "<40 hex>", "version": "4.9.0",
  "by": { "user": "<tailnet login or shell user>", "via": "elaia", "ip": "<address>" },
  "reason": "not-now", "janus": "not asked", "zeus": "0.1.4", "sig": "<64 hex>" }
```
`action` is `decline` or `deploy` (`janus: "allowed"`, `by.node` from Janus's answer when it gives one); `rollback` and `release` follow (#9, #12). `zeus verify-log [<file or folder>] [--key-file <secret.env>] [--quiet]` recomputes every line's HMAC and prints `<file>:<line>: ok` or `bad (<why>)` per line and a total; exit 0 when all are ok, 10 when any is bad, 1 when the key or the path is missing. The journal copy in the private config repo carries the signatures with it, so it is checked the same way on the server, where the key is: `zeus verify-log ~/src/realm/zeus/approvals`.

**What a good signature proves, and what it doesn't.**
- It proves the line is byte-for-byte what Zeus wrote with this key: a changed field, a removed `sig`, or a line made up by hand fails.
- It does not prove who approved. That was Janus's check at the moment of the approval; the `by` field records what Janus said.
- It does not prove that no line is missing. Deleting a whole line, or a whole month's file, leaves the others valid. The journal's git history in the config repo is what shows a removal.
- It is only as safe as the key. Anyone who can read `secret.env` can sign a line, which is why the file must be mode 600 and stays on the server and in its backup, never in git.

## 4b. The alert file (`~/.local/state/argus-ui/alerts/zeus.json`)
The format every agent uses for Argus (argus-ui #8, as Hermes): `{ "job": "zeus-deploy" | "zeus-approve", "level": "alert" | "warn", "since": "<iso>", "message": "<one sentence>", "link": "throne" }`. `alert` makes Argus glare and counts in his badge; `warn` shows on the card only. A warning never replaces an alert that is still up.
| When | Level | Message (example) |
|---|---|---|
| Janus could not verify an approval (the request is `held`) or a rollback | alert | `argus-ui 4.9.1: Janus could not verify the approval (tailscale whois failed); the request is held` |
| approved, but `systemd-run` would not start the deploy | alert | `argus-ui 4.9.1 was approved but the deploy could not be started: …` |
| a deploy failed after the install and the previous could not be restored | alert | `argus-ui 4.9.1 failed and 4.9.0 could not be restored` |
| … and there was nothing to restore (the first deploy) | alert | `argus-ui 4.9.1 failed and there is no earlier approved commit to put back` |
| a deploy failed and the previous was put back | warn | `argus-ui 4.9.1 failed and 4.9.0 was put back: …` |
| a deploy failed before the install (nothing changed) | warn | `argus-ui 4.9.1 failed before the install; nothing was changed: …` |
The file is removed by the next successful deploy and by `zeus seen` (the Throne's `seen` POST).

## 5. Zeus → Janus: `janus verify-approval`
Zeus writes one JSON object to Janus's stdin: `{ "action": "deploy", "repo": "you/argus-ui", "sha": "<40 hex>", "user": "<login>" | null, "ip": "<client address>", "when": "<iso>" }`. From the Throne, `user` is the `Tailscale-User-Login` the plugin saw and Janus checks it equals the whois user; over ssh, `user` is `null` and Janus answers with the whois user (`"user"` in its JSON), which is what the log records. Exit 0 allowed · 10 refused · 2 (or anything else) failed; Zeus reads a JSON object on stdout when there is one (`reasons`: list of sentences, `node`: the device name), else every non-empty line of stdout and stderr is a reason. Janus is given 30 s. The program is `janus` on PATH (`ZEUS_JANUS` names another, for the tests only); the deploy is started with `systemd-run` (`ZEUS_SYSTEMD_RUN` likewise).

## 6. Approving over ssh
The one-line form, from `{{PC}}` or any approved device:
```
ssh {{SSH}} 'zeus deploy argus-ui'
```
It makes a request for the current `main` (or takes the one already waiting for that commit), then asks `Deploy you/argus-ui 4.9.0 (3fa9c1b, 12 files)? type yes:` on stderr and reads one line from stdin. Only `yes` goes on; anything else, or no answer, cancels before Janus is asked. `ssh {{SSH}} 'zeus approve <id>'` does the same for a request from the Throne's list (`--sha` is optional there: the prompt shows the commit).
- Who is asking comes from the connection: the client address is the first word of `SSH_CONNECTION`, `via` is `ssh`, and the user is left to Janus's whois. `--user` or `--ip` in an ssh session is refused.
- Outside an ssh session, `--user`, `--ip` and `--sha` are the Throne's call (the plugin reads them from `tailscale serve`'s headers; the 3-second hold was the yes). Without them Zeus refuses: there is no identity to check.

## 7. Command line, exit codes
| Command | 0 | 1 | 2 |
|---|---|---|---|
| `zeus queue` | every request handled | `repos.json` broken (requests refused, folder still emptied) | — |
| `zeus request <repo>` | a pending record was made | refused or nothing fetched (unknown repository, fetch failed, not on main) | usage |
| `zeus decline <id> --reason <word>` | declined | no such request, not pending, log not writable | bad reason word |
| `zeus approve <id> --sha <sha> --user <login> --ip <addr>` (Throne) or `zeus approve <id>` (ssh) | approved, logged, deploy started | refused (no identity, `--user` over ssh, not pending, expired, wrong sha, no longer on main, not typed yes, Janus refused), held (Janus failed), or approved but the deploy did not start | usage |
| `zeus deploy <repo>` (ssh) | requested, approved, deploy started | as `request` and `approve` | usage |
| `zeus status [--json]` | printed | `repos.json` broken (`deployed` is then empty) | — |
| `zeus check-config [--file F]` | ok | errors | — |
| `zeus seen` | the alert is cleared (or none was up) | — | — |
| `zeus rollback <repo>` | approved, logged, the rollback run started | no previous, wrong target, no identity, cancelled, Janus refused or failed | usage |
| `zeus deploy-run <id>` (internal) | deployed | not approved or not signed, refused before running, failed or rolled back (reason on the record) | usage |
| `zeus verify-log [path] [--key-file F]` | every line ok | key or path missing | usage (10: a bad line) |
