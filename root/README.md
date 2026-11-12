# root/ – zeus-root (not installed yet)

zeus-root is the small root-side part of Zeus's **root actions** (`docs/design.md` §16, `docs/contract.md` §8): a short, fixed list of scripts that need root (closing a port, restarting a system service), each one run only after the owner touched a passkey on {{PHONE}}.

- `zeus_root/` is the runner and its command line. It is **not** part of the `zeus` package and never imports it. On {{SERVER}} it runs as `/usr/bin/python3 -I` from root-owned files, with the standard library and `cryptography` only. Every path, account, clock and outside command goes through one `Ctx` (`zeus_root/ctx.py`): `Ctx.system()` uses constants only, and tests build their own on temporary folders.
- `canon.py` holds the pure checks: canonical JSON (ASCII only, shared with the key page, not with `zeus/log.py`), strict parsing, base64url, the challenge, key refs and run ids.

**Nothing here is installed or running.** The slices build it in order: Z2 the runner and the command line, Z3 the gate and the units, Z4 the key page, Z5 the installer. Code under `root/` never merges by itself: each slice gets a fresh-context hostile review and the owner's yes in the chat (`CLAUDE.md`).

Tests: `bash tests/run.sh root_canon` (and the other `root_*` tests). A test that needs Linux or `cryptography` skips elsewhere, except in CI, where `ZEUS_ROOT_TESTS_REQUIRED=1` turns a skip into a failure.
