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
- `state`: `pending` or `held` (in `queue/`), then one of `approved`, `deploying`, `deployed`, `failed`, `rolled-back`, `expired`, `superseded`, `declined`, `refused` (in `queue/.done/`, never deleted).
- `reason` (text) is present on every non-pending state that stopped the request: `refused` (why), `superseded` ("replaced by 4.9.1", plus `superseded_by: {id, version, sha}`), `expired`, `declined` (the reason word).
- `files` is the number of paths changed `previous..sha` (the commit's own change when `previous` is unknown; `null` when git could not say); `rules_changed` lists those among the repository's `rules`.
- `approval`: `null` until decided; a decline sets `{ "decision": "declined", "by": {…}, "at": "…", "reason": "<word>" }`; an approval sets `{ "decision": "approved", "by": { "user", "ip", "via", "node" }, "at": "…", "janus": "allowed", "sig": "<the log line's sig>" }`.
- `janus`: after an approval attempt, `{ "verdict": "allowed" | "refused" | "failed", "reasons": [...], "at", "user", "ip" }`. A refusal leaves the record `pending` with the reasons kept for the card; a failure makes it `held` (still in `queue/`, approvable again once Janus answers) and writes the alert file `~/.local/state/argus-ui/alerts/zeus.json` (`{ "message", "link": "throne", "at" }`).
- `deploy`: `null` until approved; then `{ "started": true, "unit": "zeus-deploy-<id>", "at" }` or `{ "started": false, "error": "…", "at" }` when `systemd-run` refused (the approval stands and is logged; the alert file says so).

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

## 4. The signed log line (`log/approvals-YYYY-MM.jsonl`)
One JSON object per line, keys sorted. `sig` is HMAC-SHA256, hex, over the line's canonical JSON **without `sig`** (keys sorted, `,` and `:` separators, no spaces, UTF-8), keyed by `ZEUS_LOG_KEY` (`~/.config/zeus/secret.env`, mode 600, made by `zeus init`).
```json
{ "t": "2026-10-21T09:05:00-06:00", "id": "20261021-090012-argus-ui-3fa9c1b", "action": "decline",
  "repo": "you/argus-ui", "sha": "<40 hex>", "version": "4.9.0",
  "by": { "user": "<tailnet login or shell user>", "via": "elaia", "ip": "<address>" },
  "reason": "not-now", "janus": "not asked", "zeus": "0.1.4", "sig": "<64 hex>" }
```
`action` is `decline` or `deploy` (`janus: "allowed"`, `by.node` from Janus's answer when it gives one); `rollback` and `release` follow (#9, #12). `zeus verify-log` (#7) re-checks every line; the journal copy in the private config repo carries the signatures with it.

## 5. Zeus → Janus: `janus verify-approval`
Zeus writes one JSON object to Janus's stdin: `{ "action": "deploy", "repo": "you/argus-ui", "sha": "<40 hex>", "user": "<login>", "ip": "<client address>", "when": "<iso>" }`. Exit 0 allowed · 10 refused · 2 (or anything else) failed; Zeus reads a JSON object on stdout when there is one (`reasons`: list of sentences, `node`: the device name), else every non-empty line of stdout and stderr is a reason. Janus is given 30 s. The program is `janus` on PATH (`ZEUS_JANUS` names another, for the tests only); the deploy is started with `systemd-run` (`ZEUS_SYSTEMD_RUN` likewise).

## 6. Command line, exit codes
| Command | 0 | 1 | 2 |
|---|---|---|---|
| `zeus queue` | every request handled | `repos.json` broken (requests refused, folder still emptied) | — |
| `zeus request <repo>` | a pending record was made | refused or nothing fetched (unknown repository, fetch failed, not on main) | usage |
| `zeus decline <id> --reason <word>` | declined | no such request, not pending, log not writable | bad reason word |
| `zeus approve <id> --sha <sha> --user <login> --ip <addr>` | approved, logged, deploy started | refused (not pending, expired, wrong sha, no longer on main, Janus refused), held (Janus failed), or approved but the deploy did not start | usage |
| `zeus status [--json]` | printed | `repos.json` broken (`deployed` is then empty) | — |
| `zeus check-config [--file F]` | ok | errors | — |
