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


FIXTURES = Path(__file__).resolve().parent / "root_fixtures"
SCRIPT = "#!/bin/sh\n# test script for {name}\necho ZEUS_STAGE=change\nexit 0\n"


def install_list(ctx, change=None):
    """Write the fixture list into ctx.etc/actions.json: each entry's test script goes into libexec/actions and its real
    sha256 into the entry. change(doc) may edit the document first. Returns the exact bytes written."""
    import hashlib
    import json
    doc = json.loads((FIXTURES / "actions.json").read_text(encoding="utf-8"))
    for entry in doc["actions"]:
        body = SCRIPT.format(name=entry["id"]).encode()
        (ctx.libexec / "actions" / entry["script"]).write_bytes(body)
        entry["sha256"] = hashlib.sha256(body).hexdigest()
    if change:
        change(doc)
    raw = json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8")
    (ctx.etc / "actions.json").write_bytes(raw)
    return raw


def install_config(ctx, change=None):
    import json
    doc = json.loads((FIXTURES / "config.json").read_text(encoding="utf-8"))
    if change:
        change(doc)
    (ctx.etc / "config.json").write_bytes(json.dumps(doc).encode())
    return doc


def make_key(action="close-media-lan", **over):
    """A Key with random ids (no real key material: the public key bytes are placeholders here)."""
    from zeus_root import canon
    from zeus_root.config import Key
    cid = os.urandom(32)
    fields = dict(id=cid, ref=canon.key_ref(cid), action=action, user=os.urandom(16), node_id="nTEST1CNTRL",
                  node_name="phone", login="owner@example.com", alg=-7, spki=os.urandom(91), be=True, claim="synced",
                  words="apple brook cider delta ember", label="phone", made="2026-11-12T08:00:00+00:00", revoked=None)
    fields.update(over)
    return Key(**fields)


def good_tree(ctx):
    """Give the tree under ctx the owners' modes of design §16.3 (the current user plays root and the gate)."""
    from zeus_root import config
    config.save_keys(ctx, {})
    config.save_owners(ctx, [])
    modes = {ctx.etc: 0o755, ctx.etc / "config.json": 0o644, ctx.etc / "actions.json": 0o644, ctx.run: 0o755,
             ctx.run / "inbox": 0o700, ctx.run / "enrol": 0o700, ctx.run / "work": 0o700,
             ctx.run / "quarantine": 0o700, ctx.run / "results": 0o755, ctx.lib: 0o700, ctx.log: 0o755,
             ctx.libexec: 0o755, ctx.libexec / "actions": 0o755}
    for p in (ctx.libexec / "actions").iterdir():
        modes[p] = 0o755
    for p, m in modes.items():
        os.chmod(p, m)
