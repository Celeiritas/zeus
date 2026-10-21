# Changelog

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
