# Zeus – contracts
What crosses Zeus's borders: the message Hermes drops, the record Zeus keeps, the document the Throne page reads, and the signed log line. Times are ISO 8601 with the server's offset. Repositories are `owner/name`; commits are 40 hex characters, shown as 7 on the page. `{{SERVER}}`-style placeholders in examples are filled in from `~/.config/leafy/realm.json` on the server.

## 1. Hermes → Zeus: the deploy-request message
`hermes-github` writes `<name>-<sha7>.json` into `~/.local/share/zeus/requests/` (Hermes route `deploy-request`). Zeus treats it as a hint: he fetches `main` of the allowlisted url himself and checks the commit is on it before anything else.
```json
{ "type": "deploy-request", "repo": "you/argus-ui", "sha": "<40 hex>", "previous": "<40 hex or null>",
  "subject": "Mentor: Learn phase", "version": "4.9.0", "checks": "success",
  "changelog": ["- Learn cards …"], "seen": "2026-10-21T09:00:00+00:00" }
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
  "seen": "2026-10-21T09:00:00+00:00", "expires": "2026-10-24T09:00:00+00:00",
  "verified": { "on_main": true, "at": "2026-10-21T09:00:12+00:00" },
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
{ "zeus": "0.1.4", "at": "2026-10-21T09:05:00+00:00",
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

## 3a. The decision inbox (0.5.0): `queue/ask-<time>-<project>.json`, `zeus asks --json`, `api/throne/summary`
The record, its states (`open` → `answered` | `expired`), its signature and its log lines are in `docs/design.md` §14. What a session relies on:
- `zeus asks --json` (open asks) and `zeus asks --all --json` / `zeus asks <id> --json` print a JSON list of records, newest first, expiry applied. Only records whose signature matches are listed: one changed outside Zeus is left out and named on stderr (run `zeus verify-log`); stdout stays a JSON list. An answered record has `"state": "answered"` and `"answer": { "option", "index", "by": { "user", "via", "ip"? }, "at" }`. `via` is `elaia` for a tap on the Throne (a tailnet login and address), `ssh` or `local` for a shell; `--user`/`--ip` are refused over ssh.
- The answer is data. Zeus runs nothing because of it, and neither may the session: it reads the option text as the owner's choice and acts within its own rules.
- `GET <base>/api/throne/summary` → `{ "asks": <open asks>, "deploys": <requests waiting for a yes>, "needs_you": <both>, "line": "<the status line>" }`. Only counts and the line; the shape only grows.
- `POST <base>/api/throne/answer { "id": "ask-…", "index": 1..8 }` → the plugin runs `zeus answer <id> --index <n>` with who is asking from `tailscale serve`'s headers, and returns `{ ok, code, msg }` with what Zeus said. A request without a `Tailscale-User-Login` header is refused with 403 `{ ok: false, msg }` and Zeus is not run.

## 3b. Staged packs (0.6.0): `zeus stage-request`
The whole design is `docs/design.md` §15. What crosses the border:
- **The session gives** `zeus stage-request <repo> --version <v> --sha <40 hex> [--tests "<one line>"]`, run on the server after staging `<staging>/<v>-<sha>.tgz` and `<staging>/<v>-<sha>.tgz.sha256` (`<staging>` is the repository's fixed folder in `repos.json`). No path, file name or command is ever part of a request. Exit 0 pending · 1 refused (a `refused` record says why) · 2 bad input (nothing read).
- **The record** is §2's plus `"kind": "staged-pack"`, `"tests"` (the session's note, shown as text) and `"pack": { "name", "size", "mtime_ns", "sha256", "files", "unpacked", "checked_ns" }`. `checks` is `none`. Zeus acts on nothing in `tests`.
- **The signed approval line** gains `"kind": "staged-pack"` and `"pack": { "sha256", "size" }`; `deploy-run` installs only a pack whose sha256 is the signed one.
- **`state/<owner>--<name>.json`** entries of a staged-pack repository carry `"pack": "<sha256>"`.
- **The install script** gets `install <Zeus's copy> <version> <sha>` or `rollback <version> <sha>` and `ZEUS_ID`, `ZEUS_SHA`, `ZEUS_VERSION`, `ZEUS_PREVIOUS`, `ZEUS_PREVIOUS_VERSION`, `ZEUS_PACK_FILES`, `ZEUS_PACK_SHA256`; it prints `ZEUS_STAGE=install` before its first change (§4a's table applies unchanged).
- The Throne's `GET api/throne` passes the records as they are (with `kind`, `pack`, `tests`), and each `deployed` row gains `"kind": "git" | "staged-pack"` from `repos.json`, for the wording only (and, since 0.7.0, `running` and `drift`: §3c).

## 3c. What really runs (0.7.0): `running`, `drift`, `stale`
Zeus's own record (§4a) says what *he* deployed last. A version installed some other way (by hand, allowed) leaves that record behind, so the Throne also asks the program itself. New keys only; every old key keeps its meaning.
- **`GET api/throne` → each row of `deployed`** gains `"running": { "version": "4.36.0" } | null` and `"drift": "same" | "newer" | "older" | "no-record" | "unknown"`. `running` comes from the repository's `health.http` address in `repos.json` (the whole address must be `http://127.0.0.1:<ASCII port>/<printable ASCII>`, re-checked by the plugin), asked with one GET, no redirect followed, 1 second in all, at most 64 KB read, the JSON answer's `version` (ASCII numbers, letters, `.`, `+`, `-`; 40 characters at most; nothing after them, not even a newline), kept 30 seconds. Anything else (another address, no answer, a redirect, not JSON, no `version`) is `null`. `drift` compares `running` with `deployed.version` as numbers (`4.33` equals `4.33.0`): `newer` = what runs is newer than Zeus's last deploy; `no-record` = something answers and Zeus has no record; `unknown` = nothing answered or a version is not plain numbers. A POST never asks.
- **Each record in `waiting`** gains `"stale": true | false`: true when its version is older than `running` of its repository (both plain ASCII numbers). An equal version is not stale: it says nothing about which commit runs. A stale request needs nobody: it is left out of `summary.deploys`, `summary.needs_you` and `status.needs`/`count`/`action`, the page offers no Deploy for it, and it expires by itself. Zeus himself does not know the word: `zeus approve` would still accept it until it expires (§6, unchanged).
- **`line`** names what runs when it is known (`argus-ui 4.36.0 running`) and otherwise Zeus's last deploy (`argus-ui 4.33.0 deployed`; before 0.7.0 both read `… live`). Stale requests read `1 old request, expires Fri` (`2 old requests, expire by Fri`).
- **`GET api/throne/summary`** gains `"stale": <waiting requests that are stale>`.

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
{ "t": "2026-10-21T09:05:00+00:00", "id": "20261021-090012-argus-ui-3fa9c1b", "action": "decline",
  "repo": "you/argus-ui", "sha": "<40 hex>", "version": "4.9.0",
  "by": { "user": "<tailnet login or shell user>", "via": "elaia", "ip": "<address>" },
  "reason": "not-now", "janus": "not asked", "zeus": "0.1.4", "sig": "<64 hex>" }
```
`action` is `decline`, `deploy` or `rollback` (the last two with `janus: "allowed"` and `by.node` from Janus's answer when it gives one; a `rollback` line also has `from`, §4a); the decision inbox adds `ask`, `answer` and `ask-expired` lines (§3a); `release` follows (#12). `by.user` of a `deploy` or `rollback` line is the login Janus verified: from the Throne the login the page passed (Janus refuses when whois says another); over ssh the one Janus read from whois, or `null` when he allowed it without naming one. Zeus never fills in the shell's user there. `zeus verify-log [<file or folder>] [--key-file <secret.env>] [--quiet]` recomputes every line's HMAC and prints `<file>:<line>: ok` or `bad (<why>)` per line and a total; exit 0 when all are ok, 10 when any is bad, 1 when the key or the path is missing. The journal copy in the private config repo carries the signatures with it, so it is checked the same way on the server, where the key is: `zeus verify-log ~/src/realm/zeus/approvals`.

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
- What Zeus prints after the yes (stdout, one line): `<id>: approved (you/argus-ui 4.9.0) by <login>; deploying as zeus-deploy-<id>`, where `<login>` is the signed line's `by.user` (over ssh, the whois login Janus answered with). When Janus allowed it without naming a login, the middle reads `from <device> (Janus named no login)`, or `from <address> (Janus named no login)` when he named no device. `zeus rollback` prints `<id>: rollback of you/argus-ui 4.9.1 → 4.9.0 approved by <login>; running as zeus-deploy-<id>` with the same rule. A login or device name that is empty or only spaces counts as none. (Before 0.6.1, `approve` and `deploy` printed `by None` over ssh; `rollback` printed the login, and `by None` only when Janus named none.)

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
| `zeus verify-log [path] [--key-file F]` | every line (and, by default, every ask record in `queue/`) ok | key or path missing | usage (10: a bad line or record) |
| `zeus ask --project P --title T --option A --option B [--context C] [--expires 3d] [--json]` | asked: the id (or the signed record) printed | no key | bad fields (all of them named) |
| `zeus answer <id> <option>` / `zeus answer <id> --index N [--user L --ip A]` | answered, once | no such ask, not open (answered, expired), changed outside Zeus, no key | bad id, option or index; `--user`/`--ip` over ssh, or one without the other |
| `zeus asks [<id>] [--open\|--all] [--project P] [--json]` | listed (a record changed outside Zeus is left out and named on stderr) | no key | usage |

## 8. Root actions from the phone (design §16): records, results, the log line, the attention line
**Status: root's checks are built (0.8.0, `root/zeus_root/`; the re-check's fixes in 0.8.1); nothing is installed and nothing runs yet.** Z2a reads and checks everything (the record, the assertion, the list, the keys, the inbox entries) and runs nothing; the runner is Z2b, the commands Z2c, the install Z5. The whole design is `docs/design.md` §16 (tier R in §14). This section is what crosses the borders between the key page, the gate, the runner and the readers of root's log. Times are ISO 8601 with the server's offset (the examples show `+00:00`). Placeholders in examples are filled in on the server. Where the private design names a fact but not yet a field, the text says "fixed in Z2" (or the slice that builds it) and that slice writes the shape here before it merges.

**Never in the result file, the run log, `attention.jsonl`, the quarantine line, the journal or any file a normal-user process can read:** a key id or user handle (only the key ref: the first 16 hex digits of the SHA-256 of the key id), a secret, a home path. The ids do exist in the inbox record (8b), the gate's answers (8c) and `keys.json`, and only there; the inbox, `work/`, `quarantine/` and `keys.json` (root:`zeus-root-gate` 640) are not readable by the normal user. The owners' file has the same mode; `labels.json` (root 644) holds no ids. The full table of owners and modes is `docs/design.md` §16.3.

### 8a. The list entry (`/etc/zeus-root/actions.json`, one entry; the real file is private, the repository carries an `.example`)
```json
{ "id": "close-media-lan", "kind": "change",
  "title": "Close the media ports to the apartment network", "sheet": "Zeus · Close the media ports",
  "button": "Close the 3 ports", "icon": "lock",
  "changes": [["Jellyfin, Audiobookshelf, the downloader's page", "open on the apartment Wi-Fi and cable", "closed there"],
              ["Through Tailscale (your phone, your PCs)", "", "no change"]],
  "script": "close-media-lan-v2.sh", "sha256": "<64 hex>", "undo": "open-media-lan",
  "game_check": true, "can_cut_you_off": true, "ask_hours": 24,
  "max_per_day": 6, "min_gap_s": 120, "added": "zeus-root 1.0.0" }
```
| Field | Must be |
|---|---|
| `id` | the action's name; the only thing a page, a message or a record may name |
| `kind` | `check` (changes nothing), `change`, or `put-back` (restores the state from before a change) |
| `title`, `sheet`, `button`, `icon`, `changes` | the words of the key page and of Android's sheet; `changes` rows are `[what, before, after]` |
| `script`, `sha256` | a file in `actions/` and its checksum; root re-hashes before every run |
| `undo` | the id of the paired entry, or `null` (a restart: nothing to put back). `null` is accepted only with `can_cut_you_off: false` |
| `game_check` | refuse the start while a game runs (and delay a put-back by at most `reach_game_max_s`, default 900) |
| `can_cut_you_off` | true starts the reach check after a successful run |
| `ask_hours` | for Zeus's ask only; root ignores it |
| `max_per_day`, `min_gap_s` | the limits of design §16 (v1: 6 a day, 2 minutes apart) |

### 8b. The record (what the gate writes into root's inbox, `/run/zeus-root/inbox/`)
One file per approved try, written under a temporary name and renamed into place, at most 16 KB, one JSON object with exactly its keys and no repeated key. Root treats the inbox as hostile: every entry is moved out first, opened without following links and checked on the open handle before it is read.
```json
{ "type": "run", "action": "close-media-lan", "params": {}, "list": "<sha256 hex of actions.json>",
  "nonce": "<32 random bytes, base64url>", "exp": 1794000000,
  "credentialId": "<b64url>", "clientDataJSON": "<b64url>", "authenticatorData": "<b64url>",
  "signature": "<b64url>", "userHandle": "<b64url>",
  "seen": { "login": "<login>", "node": "<phone device name>", "node_id": "<stable node id>", "ip": "<address>" } }
```
- **Exactly these twelve keys**, every one always present (Z2, 2026-11-11). `action`, `params`, `list`, `nonce` and `exp` travel in the record: they are what the gate issued at `begin` and the phone signed; root never takes the origin from the record. `params` is `{}` for a run and exactly `{ "key": "<key ref>" }` for `check-key`. `nonce` is 43 characters of base64url (32 bytes), `action` an id of `a-z`, `0-9` and `-` (1 to 40), `list` 64 lowercase hex, `exp` an integer from 1 to 2^53 − 1 (the largest whole number a page's JavaScript holds exactly): a field outside these shapes is refused as malformed before anything is hashed, so the page and root can never serialise one record two ways.
- The five assertion fields (`credentialId`, `clientDataJSON`, `authenticatorData`, `signature`, `userHandle`) are what the phone returned (`finish` of the gate, whose sixth key is the nonce), in base64url without padding. `userHandle` may be `null` (a key that returns none is then refused). `seen` is what the gate saw; root takes it as the gate's word, not as proof. `node_id` is Tailscale's stable node id from `tailscale whois`, and the device check compares it, never the name.
- `type` is `run` for an action or `check-key` for the Self-test's check of one key (then `"action": "check-key"`; root accepts any registered key of the asking device for it, only when `params.key` names that very key, and runs nothing). A check-in is `{ "type": "reach", "run": "<run id>", "seen": { … } }`, exactly those three keys; root accepts it only while that run waits and when `seen.node_id` and `seen.login` equal the approver's (built in Z2b).
- Root recomputes the challenge from the record and its own copy: SHA-256 of `zeus-root/v1\n` and the canonical JSON (keys sorted, no spaces, ASCII only) of `{ "action", "params", "list", "nonce", "exp", "origin" }`, with the key page's exact origin from `config.json` (`https://<rp_id>:<keyport>`; `keyport` is 1025 to 65535 and never 8443 or 10000, the ports Tailscale Funnel can open to the internet). The record carries `list` so that root can tell a yes given before the list changed (refused `list-changed`, after the signature checks out) from a forged challenge (refused `challenge`, counted). `exp` is at most 180 s after issue.
- **The run id** is `"r"` and the first 20 hex digits of SHA-256(`zeus-root/run\n` + nonce). The gate computes the same for `finish`, so both agree without another field, and a repeat of one nonce gives the same run.
- **What root checks, in this order** (`docs/design.md` §16.4 step 6): the nonce's shape; the rest of the record's shape; the key (registered, not revoked, for this action, this user handle) and the device; the client data (type `webauthn.get`, exact origin, not framed, root's challenge); the RP ID hash; user present and user verified; **the signature**; then freshness (`now < exp ≤ now + 180`); then whether the nonce was spent before (refused `used-nonce`); then the counter and the BE flag; then the list. The signature, freshness and the nonce come before the counter and BE on purpose: a record nobody signed, and a genuine record sent late or sent again, can never raise "your key may be copied" (8f). `used-nonce` is decided only at its place in this order, so a record refused for an earlier fault keeps that reason: a forgery stays counted even when its nonce was spent.
- **Every answer spends the nonce** (0.8.1). Root marks a record's nonce used whatever it answers, for every record that reaches the assertion: accepted; refused for any reason (the runner's later reasons come after the assertion has spent it); or malformed in any other part. A record refused as busy at the lock never reaches the assertion, so the runner spends its nonce itself (Z2b), for a day, without its `exp` (its signature was not checked) and without the lock: a new nonce file appears only with its time, and a spend without a signed `exp` never changes one that is there, so it cannot shorten a keep set under the lock. Root keeps a spent nonce a day, or, when the record's signature checked out, until a day after the later of that moment and the record's `exp`: only then is that `exp` the one the phone signed (the challenge commits to it). A nonce already spent is kept longer, never shorter, when a later copy's signature checks out with an `exp` past the nonce's time that has not passed yet (a record past its `exp` is never fresh again). Before the signature the `exp` is only the gate's word, and a far one would keep a nonce file for years. So once root has checked a record's signature, the record can never run later when the gate sends it again as it was, even when its `exp` was too far ahead and a spoiled or busy copy came first; and a spoiled copy of a record inside its 180 s cannot let the intact one run later either: a genuine resend inside its 180 s is `used-nonce`, and a record that gets past the nonce is the first answer for it. **Root leaves one case to the key page:** a record whose `exp` is more than a day ahead that root refused only before checking its signature while it kept the nonce: spoiled copies (any part spoiled, its `exp` moved or not; refused counted, or as garbage when the gate spoils the key or the device) or copies refused busy at the lock. Root then keeps its nonce one day and never learns the signed `exp`, and the intact record could run once that `exp` comes near. Only the key page's check below stops that, so Z5 installs nothing before that check is built and tested (Z4). A result that says `refused` (8d) is written only after its nonce is spent. Two kinds of entry spend nothing, and no result is written for either, so the key page is never told "Nothing ran" about them: a record whose nonce itself has the wrong shape (it names no run: the run id comes from the nonce), and an inbox entry quarantined before the assertion (the file rules, or the strict parse of its keys and types).
- **The key page checks `exp` before it asks for the fingerprint** (built in Z4). It signs nothing when the `begin` answer's `exp` is not after its own clock or is more than 240 s ahead of it (the 180 s of a challenge plus 60 s for clocks that disagree); it says so in one sentence instead. Root's own window (`exp ≤ now + 180`) cannot catch this alone: a lying gate could issue an `exp` days away, keep the genuine signature and hand it in within 180 s of that `exp`, at a time of its choosing. Root's nonce keep closes this once root has checked the intact record's signature while it keeps the nonce; when the gate shows root only spoiled or busy copies until the nonce is forgotten, this check is the only one (above). The page comes from root's page folder through Serve, not from the gate (§16.4 steps 2 and 4), so the gate cannot take the check out.

### 8c. The gate's calls (the key page's `POST` requests, JSON, `Content-Type: application/json`)
| Path | Request | Answer |
|---|---|---|
| `/api/begin` | `{ "action", "params"? }` (`params` only for `"action": "check-key"`, where it is `{ "key": "<key ref>" }`; otherwise absent or `{}`) | `{ "nonce", "exp", "list", "origin", "challenge", "rpId", "allowCredentials": [<this action's keys for the asking device>], "userVerification": "required", "timeout": 60000 }` |
| `/api/finish` | `{ "nonce", "credentialId", "clientDataJSON", "authenticatorData", "signature", "userHandle" }` | `{ "run" }` (a repeat for the same nonce returns the same run and writes nothing) |
| `/api/result` | `{ "run"? }` | the result of 8d plus the run log's `ZEUS_STEP=` lines; with no run, this device's run that is waiting for a check-in |
| `/api/reach` | `{ "run" }` | the same answer as `result` |
| `/api/history` | `{}` | the last 10 lines of the run log, without ids |
| `/api/enrol` | the registration submission (only while root has a window open) | fixed in Z3 |
Every answer carries `Content-Type: application/json`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`, and no `Access-Control-*` header. A refusal is one short JSON object `{ "ok": false, "msg": "<one sentence>" }` (the sentence's wording is fixed in Z3). Any method but `POST` gets 405.

### 8d. The result (`/run/zeus-root/results/<run>.json`, root 644, no ids, no secrets)
One file per run, rewritten as the run moves on (written to a temporary name and renamed into place). **Always exactly these keys**, `null` when not used (Z2): `run, action, state, sentence, started, updated, deadline, remind_at, undo_ran, undo_ok, class, zeus_root`. `state` is one of `running, waiting, done, stopped, failed, undone, superseded, refused`; `class` is the refusal's class (8e) for `refused`, else `null`; `deadline` and `remind_at` are set while a reach check waits; `zeus_root` is root's version. A refusal never replaces an existing result: a replayed record has the same run id, and the first run's result stays. What the fields carry:
- the run id, the action id and the times;
- the state: running, `waiting for your phone` (a reach check is open; carries the deadline), `done`, `stopped` (before the change), `failed` (after it, with whether the undo ran and whether it worked), `undone` (put back by itself), `superseded`, or `refused` (with the reason);
- for a refusal, a failure or a put-back, one plain sentence for the key page, written by root, not by the script. "Nothing ran" is said only when this file says so.
The steps shown while it runs come from the script's own `ZEUS_STEP=<n>/<total> <sentence>` lines in `run-<id>.log` (world-readable, no key id); `ZEUS_STAGE=change` marks the point after which a failure runs the undo. Exit codes of a script: 0 done · 2 stopped before the change · 3 not as expected after it; any other non-zero exit before `ZEUS_STAGE=change` is `stopped` (nothing changed), and after it a failure that runs the undo once; an entry with no undo reports that the service did not come back.

### 8e. The log line (`/var/log/zeus-root/runs-YYYY-MM.jsonl`, root 644)
One JSON object per try, readable by everyone on `{{SERVER}}`, so it holds no key id, user id or secret:
```json
{ "t": "2026-11-12T08:05:11+00:00", "run": "<id>", "action": "close-media-lan", "outcome": "done",
  "exit": 0, "secs": 4.2, "undo_ran": false, "reach": "kept",
  "key": { "ref": "<first 16 hex of sha256(key id)>", "label": "phone", "device": "{{PHONE}}", "synced_claimed": true },
  "uv": true, "seen": { "login": "<login>", "node": "<node>", "ip": "<address>" },
  "assertion": { "clientDataJSON": "<b64url>", "authenticatorData": "<b64url>", "signature": "<b64url>" },
  "challenge": "<hex>", "list": "<hex>", "zeus_root": "1.0.0", "prev": "<sha256 of the previous line>" }
```
| `outcome` | Meaning |
|---|---|
| `done` | the script ran and exited 0 |
| `stopped` | it stopped before the change |
| `failed` | it failed after the change; `undo_ran` says what followed |
| `undone` | put back by itself, with a reason: no check-in · no check-in, after a restart · no check-in, after 15 minutes of game |
| `superseded` | a pair's newer run replaced a waiting check |
| `key-checked` | the Self-test's check of one key |
| `refused` | with the reason and its class: **counted toward the pause** (bad signature, wrong origin, framed, UV missing, challenge mismatch); **not counted** (expired, used nonce); **garbage, counted apart** (unknown or revoked key, a key for another action, a wrong user handle, wrong device, malformed); **other** (counter, BE changed, list changed, file rules, Serve mapping changed, rate limit, paused, game running with the matching process named, busy; some of these write an attention line, see 8f) |
- **One line per try, written when it is final** (Z2). A run that waits for a check-in keeps its line in its reach file (root only) and writes it when the wait ends, with `reach` = `kept`, `undone` or `superseded`; the undo of a put-back writes its own `undone` line. `reach` is absent for every other line. `prev` makes a hash chain across the monthly files: a changed or removed line shows.
- A refused line carries `reason` and `class`. The reasons of the assertion check (Z2a): `unknown-key`, `revoked-key`, `wrong-action-key`, `wrong-user`, `wrong-device` and `malformed` (garbage); `challenge`, `origin` (the client data's origin or the RP ID hash), `framed` (`crossOrigin` true or a `topOrigin`), `uv` (user present or verified missing) and `signature` (counted); `expired` and `used-nonce` (not counted: a genuine record sent late, or sent again after root answered or refused it); `counter`, `be-changed` and `list-changed` (other). The runner's own reasons (Serve mapping, rate limit, paused, game, busy, a changed script) are fixed in Z2b.
- **The whole assertion is kept**, so `zeus-root verify-log` re-checks every signature against the registered public keys (found by `key.ref`), not just the chain. Unlike Zeus's HMAC log (§4), only the phone could have made those signatures. The assertion holds neither the key id nor the user handle. Anyone who reads the log can derive the registered public key from the signatures kept there (two ES256 signatures give one key; two RS256 signatures give the modulus). That is accepted: a WebAuthn public key is not a secret, and it lets nobody sign.
- The quarantine writes one line per entry, never its content: `{ "t", "run": null, "outcome": "refused", "class", "reason", "entry": { "type", "size", "uid", "sha256_16k" } }` plus `zeus_root` and `prev`. **The file rules come first**: an entry that is not a regular file, is not owned by the gate, has more than one link or is over 16 KB is `class: other, reason: file-rules` with a short `why`, even when its content is also bad JSON. An entry that passes the file rules but does not parse as a record is `class: garbage, reason: malformed`; a record left in `work/` by a crashed start is `class: other, reason: leftover`. `type` is `file`, `folder`, `link`, `fifo`, `socket`, `device` or `other`.

### 8f. The attention line (`/var/log/zeus-root/attention.jsonl`, root 644)
One line for each of: a put-back by itself, a failed undo, a restart that did not come back, a pause, a quarantine flood, a refusal of the class counted toward the pause (8e), a refusal for the counter or a changed BE flag (signs of a copied key), a refusal because the list changed, a refusal because the Serve mapping changed, a refusal because an inbox entry breaks the ownership and mode rules (at most one line an hour for that kind; later ones in the hour are counted by root, and the next line of that kind says how many in `folded`), and the watcher unit failing. The file rules are checked first, so an entry that breaks them is a file-rules refusal (it writes this line) even when its content is also malformed. A refusal in the classes **garbage** or **not counted** never writes an attention line (that would let anyone who can send garbage make the phone buzz again and again); it goes to the log only, and at 20 garbage records in an hour Elaia shows the owner one warning (a warning on the page, not a buzz). The rest of the **other** class goes to the log only: a refusal for a rate limit is shown on the key page where the owner pressed (only his own logins and devices get that far); a refusal while paused is counted and shown with the pause, which already wrote its own line; game and busy are expected. The rule behind this list (the owner, 2026-11-11): a line must bring the owner new information and call for an action of his, and only the cause writes a line, never each refusal that follows from it. Elaia's side turns each new line into one `broke` event: a buzz of the kind *Something stopped working*, and Argus glares. The words come from root, one sentence each, for example:
- `Close the media ports was put back: your phone did not check in`
- `Zeus refused 3 tries`
- `Restart Bluetooth: the service did not come back up`
A reminder at minute 5 of a waiting check-in is not an attention line; it is read from the result (8d) and raised as *Needs your yes: keep Close the media ports*, with a body ending *Open it to check in from this phone, or it is put back at 08:51*. No key id, user id or secret appears in it.

**The line (Z2):** exactly `{ "t", "run", "kind", "sentence", "folded" }`. `run` is the run id, or `null` when there is none (an inbox entry, a flood, the watcher). `folded` is how many lines of that kind root held back since the last one (always `0` except for `file-rules` and `watcher-failed`, the two kinds limited to one line an hour; root keeps the count in its own state, never by rewriting a line). `kind` is one of these, in the order Elaia ranks them, most urgent first: `copied-key` (a counter that did not go up, or a changed BE flag, both only for a record whose signature checked out, that is fresh and whose nonce was never spent), `pause`, `refused-counted`, `flood`, `serve-changed`, `list-changed`, `undo-failed`, `not-back` (a restart that did not come back), `put-back`, `file-rules`, `watcher-failed`.

**Only the cause writes a line (Z2):** the counted refusal that makes the third in an hour writes the `pause` line ("Zeus refused 3 tries; root changes are paused for an hour") instead of its own `refused-counted` line; a flood writes `flood` and no separate `pause` line; a refusal while paused writes none.

### 8g. Zeus's ask, tier R (`zeus root-ask <id>`, built in Z6)
A tier R ask is a record like §3a's, with `kind` `root`, `tier` `"R"` and the entry's `title` copied from `/etc/zeus-root/actions.json`; Zeus refuses an id that is not in the list, and sets the expiry from the entry's `ask_hours`. It carries no path, command or script, and it can make nothing run: the yes is the owner's signature on the key page, verified by root. Fixed in Z6: the exact record and the Throne's reading of root's results and log.
