"""zeus-root's command line (Z2b part): `run`, `reach-tick <run>`, `boot`, `version`. Z2c adds `enroll`, `rebind`,
`revoke`, `keys`, `pause`, `resume` and `verify-log`.

On {{SERVER}} it runs as root from root-owned files (`/usr/bin/python3 -I`), started by zeus-root-run.service,
zeus-root-reach@<run>.service and zeus-root-boot.service. Before anything else it clears its environment to PATH alone
(plan v2, finding 7: sudo can pass TZ, OPENSSL_CONF and more), refuses unless it runs as root, and builds the one Ctx
from constants (Ctx.system()). Tests pass their own Ctx and keep their environment.

Exit codes: 0 done (a refusal is an answer, not an error) · 1 failed (not root, not installed, a state file that does
not read back, root's own files broken for a tick or boot) · 2 usage.
"""
import os
import sys

from . import __version__, logs
from .ctx import COMMAND_PATH, Ctx

USAGE = "usage: zeus-root run | reach-tick <run> | boot | version"


def _clear_environment():
    for name in list(os.environ):
        del os.environ[name]
    os.environ["PATH"] = COMMAND_PATH


def _say(text):
    print("zeus-root: " + text, file=sys.stderr)


def main(argv, ctx=None):
    if not argv or argv[0] not in ("run", "reach-tick", "boot", "version"):
        _say(USAGE)
        return 2
    cmd, args = argv[0], argv[1:]
    if (cmd == "reach-tick") != (len(args) == 1) or (cmd == "reach-tick" and not logs.RUN_RE.fullmatch(args[0])):
        _say(USAGE)
        return 2
    if cmd == "version":
        print(__version__)
        return 0
    if ctx is None:
        _clear_environment()
        if not hasattr(os, "geteuid") or os.geteuid() != 0:
            _say("run this as root (it is started by its systemd units)")
            return 1
        try:
            ctx = Ctx.system()
        except RuntimeError as e:
            _say(str(e))
            return 1
    from . import reach, runner
    try:
        if cmd == "run":
            return runner.start(ctx)
        if cmd == "reach-tick":
            return reach.tick(ctx, args[0])
        return reach.boot(ctx)
    except Exception as e:     # the unit fails, and Elaia's watch shows it; the message never quotes a record
        _say("%s failed: %s: %s" % (cmd, type(e).__name__, e))
        return 1
