"""Helpers for the zeus-root tests (root/zeus_root, design §16): the import path, need(), and a Ctx on temporary folders.

zeus-root is not part of the `zeus` package and never imports it; the tests reach it by putting `root/` first on sys.path.
"""
import os
import sys
import unittest
from datetime import timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "root"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _have(what):
    if what == "linux":
        return sys.platform.startswith("linux")
    if what == "cryptography":
        try:
            import cryptography  # noqa: F401
        except ImportError:
            return False
        return True
    raise ValueError(what)


def need(*what):
    """Skip the calling test unless every need is met ("linux", "cryptography").

    With ZEUS_ROOT_TESTS_REQUIRED=1 (set in CI) a missing need fails the test instead, so CI can never pass on skips."""
    missing = [w for w in what if not _have(w)]
    if not missing:
        return
    if os.environ.get("ZEUS_ROOT_TESTS_REQUIRED") == "1":
        raise AssertionError("ZEUS_ROOT_TESTS_REQUIRED=1 but missing: " + ", ".join(missing))
    raise unittest.SkipTest("needs " + ", ".join(missing))


class Clock:
    """A clock the test moves by hand: ctx.now() returns .t."""

    def __init__(self, t=1794000000.0):
        self.t = float(t)

    def __call__(self):
        return self.t


class FakeCmd:
    """Records every argv; answers from .answers (argv tuple -> CmdResult), else exit 0 with no output."""

    def __init__(self):
        self.calls = []
        self.answers = {}

    def __call__(self, argv, timeout):
        from zeus_root.ctx import CmdResult
        self.calls.append(list(argv))
        return self.answers.get(tuple(argv), CmdResult(0, "", ""))


def no_terminal(prompt):
    from zeus_root.ctx import NoTerminal
    raise NoTerminal("no terminal in this test")


def make_ctx(tmp, **over):
    """A Ctx whose folders live under tmp; the current user plays both root and the gate account."""
    from zeus_root.ctx import Ctx
    tmp = Path(tmp)
    uid = os.getuid() if hasattr(os, "getuid") else 0
    gid = os.getgid() if hasattr(os, "getgid") else 0
    ctx = Ctx(etc=tmp / "etc", run=tmp / "run", lib=tmp / "lib", log=tmp / "log", libexec=tmp / "libexec",
              proc=tmp / "proc", root_uid=uid, gate_uid=uid, gate_gid=gid, now=Clock(), tz=timezone.utc,
              cmd=FakeCmd(), ask=no_terminal)
    for k, v in over.items():
        setattr(ctx, k, v)
    for d in (ctx.etc, ctx.run / "inbox", ctx.run / "enrol", ctx.run / "work", ctx.run / "quarantine",
              ctx.run / "results", ctx.lib / "reach" / "done", ctx.lib / "nonces", ctx.log, ctx.libexec / "actions",
              ctx.proc):
        d.mkdir(parents=True, exist_ok=True)
    return ctx
