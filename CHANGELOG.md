# Changelog

## 0.2.2 – 2026-10-21
- `zeus verify-log [<file or folder>] [--key-file F] [--quiet]` (issue #7): recomputes the HMAC of every line in `approvals-*.jsonl`, Zeus's own or the journal copy in the config repo, and prints ok or bad (no sig, not JSON, sig does not match) per line with a total; exit 10 when any line is bad. `docs/contract.md` says what a good signature proves (the line is unchanged since signing) and what it does not (who approved, that no line was deleted).

## 0.2.1 – 2026-10-21
- The ssh approval path (issue #6): `ssh {{SSH}} 'zeus deploy argus-ui'` makes a request for the current `main` (or takes the one already waiting) and approves it; `ssh {{SSH}} 'zeus approve <id>'` approves one from the list. The address comes from `SSH_CONNECTION`, the user from Janus's whois (Janus gets `user: null`), and the owner types `yes` to the prompt "Deploy <repo> <version> (<sha7>)?"; anything else cancels before Janus is asked. `--user`/`--ip` in an ssh session are refused; outside one, `--user`, `--ip` and `--sha` (the Throne's call) are required.
- A `held` record now expires, counts as a duplicate and is superseded like a pending one.

## 0.2.0 – 2026-10-21
- `zeus approve <id> --sha <sha> --user <login> --ip <addr>` (issue #5, milestone 0.2 "the yes"): refuses unless the record is `pending` (or `held`), unexpired, the sha is the one the page showed, and the commit is still on `main` after a fresh fetch. Then `janus verify-approval` with `{action, repo, sha, user, ip, when}` on stdin: allowed → `approved`, a signed `deploy` line in the log, and `systemd-run --user --unit=zeus-deploy-<id> --collect -p CPUQuota=50% -p MemoryMax=1G -p IOWeight=50 zeus deploy-run <id>`; refused → stays `pending` with Janus's reasons on the record; failed → `held` plus the Argus alert file. A second approval of the same record is refused.
- Fakes for the tests: `tests/fixtures/janus.py` and `tests/fixtures/systemd_run.py`, named through `ZEUS_JANUS` / `ZEUS_SYSTEMD_RUN`.

## 0.1.4 – 2026-10-21
- `zeus request <repo>` (issue #4): a record now for the current `main` of an allowlisted repository, through the same admission as a Hermes message (fetch, on-main check, diff, supersede); `subject`, `version` and `changelog` lines come from the commit itself, `checks` is `none`. An unknown repository is refused without a fetch.
- `zeus decline <id> --reason <word>`: `pending` → `declined`, one signed line in `log/approvals-YYYY-MM.jsonl`, no Janus call. `log.py` signs every decision line with HMAC-SHA256 over its canonical JSON (keys sorted, no spaces, without `sig`), keyed by `ZEUS_LOG_KEY`.
- `zeus status [--json]`: what is live per repository, the queue, what is deploying, the last ten history rows, and the one-line status; the JSON shape is in `docs/contract.md`.
- `docs/contract.md`: the Hermes → Zeus message, the record, the status document, the log line.
- Pull requests merge themselves when CI is green (`CLAUDE.md`).

## 0.1.3 – 2026-10-21
- `zeus queue` (issue #3, `zeus-queue.service`): every message in `requests/` becomes a record in `queue/<id>.json` and leaves the folder, whatever happens. Zeus fetches `main` of the allowlist's url himself (into the clone at `path`, or a bare mirror under `~/.local/share/zeus/mirrors/` before the first deploy) and refuses a commit that is not on it, a repository not in `repos.json` (nothing is fetched), a broken message, and a duplicate of a waiting request. `files` and `rules_changed` come from the diff. A newer commit of the same repository supersedes the waiting one ("replaced by 4.9.1"); a request expires `expire_hours` after `seen`, enforced on listing. Terminal records move to `queue/.done/`.

## 0.1.2 – 2026-10-21
- File modes: `check-config` fails a private repository's deploy key that other users can read (`chmod 600` in the message); `zeus init` tightens a kept `secret.env` to 600 and says so, and the key reader refuses one other users can read.
- `tests/test_check_private.py` passes the denylist path with forward slashes, so it runs on a Windows PC too (`awk -v` reads a Windows path's backslashes as escapes).

## 0.1.1 – 2026-10-21
- Continuous integration, as in Hermes: `.github/workflows/tests.yml` builds the zipapp (`bash scripts/build.sh && ./dist/zeus --version`) and runs `bash tests/run.sh` on every pull request and on every push to `main`; `.github/workflows/check-private.yml` runs the generic part of `scripts/check-private.sh` on every commit a pull request adds and on the whole tree as it would be after merging. Both run with `contents: read` only.
- `tests/test_check_private.py` (ported from Hermes): the privacy check fails on a planted home path, `*.ts.net` name, IPv4, IPv6 and email, passes clean text, honours `private-ok`, checks commit messages in a range, matches the denylist ignoring case, and passes on this repository's own tree.

## 0.1.0 – 2026-10-21
- The skeleton (issue #2, v0.1 "the queue"): the `zeus` package (standard library only) with the command line from the design (`check-config` and `init` work; `queue`, `request`, `approve`, `approve-release`, `decline`, `deploy-run`, `rollback`, `status`, `verify-log` exist and say which issue builds them).
- `zeus check-config [--file F]`: reads `repos.json` (placeholders from `~/.config/leafy/realm.json`), one plain sentence per repository, plain-sentence errors. A `branch`, `script` or command field fails; `url` must be `https://github.com/<owner>/<name>.git` or `git@<ssh alias>:<owner>/<name>.git` for a private repository, and the key must equal the url's owner/name; `health` is exactly one of `http` (127.0.0.1 only), `service`, `exec` (from the fixed list).
- `zeus init`: `~/.config/zeus/secret.env` (mode 600, `ZEUS_LOG_KEY` = 32 random bytes as hex), never overwritten, plus the data folders under `~/.local/share/zeus/`.
- `scripts/build.sh` (zipapp `dist/zeus`), `scripts/install.sh` (`~/.local/bin/zeus` + the units), `tests/run.sh` (unittest; uses `python` where `python3` is missing), `units/zeus-queue.path`, `units/zeus-queue.service`, `units/argus-limits.conf`, `examples/repos.example.json`.

## 0.0.1 – 2026-10-21
- Repo created: README, CLAUDE.md, `docs/design.md` (the spec), licence, privacy check (`scripts/check-private.sh`, `scripts/install-hooks.sh`, copied from argus-ui)
