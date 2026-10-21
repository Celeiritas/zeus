# Changelog

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
