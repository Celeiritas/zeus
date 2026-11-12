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


_SPKI = {}


def spki_for(alg):
    """A real public key (DER SubjectPublicKeyInfo) that fits alg, made once per test run: ES256 a P-256 key, RS256 a
    2048-bit RSA key. Needs cryptography (config.load_keys refuses a key that does not load and fit its alg)."""
    if alg not in _SPKI:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        priv = (ec.generate_private_key(ec.SECP256R1()) if alg == -7
                else rsa.generate_private_key(public_exponent=65537, key_size=2048))
        _SPKI[alg] = priv.public_key().public_bytes(serialization.Encoding.DER,
                                                    serialization.PublicFormat.SubjectPublicKeyInfo)
    return _SPKI[alg]


def make_key(action="close-media-lan", **over):
    """A Key with random ids and a real public key that fits its alg (spki_for; pass spki= to choose one)."""
    from zeus_root import canon
    from zeus_root.config import Key
    cid = os.urandom(32)
    fields = dict(id=cid, ref=canon.key_ref(cid), action=action, user=os.urandom(16), node_id="nTEST1CNTRL",
                  node_name="phone", login="owner@example.com", alg=-7, spki=None, be=True, claim="synced",
                  words="apple brook cider delta ember", label="phone", made="2026-11-12T08:00:00+00:00", revoked=None)
    fields.update(over)
    if fields["spki"] is None:
        fields["spki"] = spki_for(fields["alg"])
    return Key(**fields)


class Spender:
    """verify()'s spend for tests: a set of spent nonces; .calls counts the calls, .exps lists the exp of each."""

    def __init__(self, spent=()):
        self.spent, self.calls, self.exps = set(spent), 0, []

    def __call__(self, nonce, exp):
        self.calls += 1
        self.exps.append(exp)
        if nonce in self.spent:
            return False
        self.spent.add(nonce)
        return True


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


FLAGS_OK = 0x01 | 0x04 | 0x08 | 0x10    # UP, UV, BE, BS: a synced passkey confirmed with the fingerprint
SERVE_STATUS = ("/usr/bin/tailscale", "serve", "status", "--json")
SERVE_PREFS = ("/usr/bin/tailscale", "debug", "prefs")
SERVE_PID = ("/usr/bin/systemctl", "show", "tailscaled.service", "-p", "MainPID", "--value")
TAILSCALED_PID = "4100"


def serve_status(cfg, **over):
    """What `tailscale serve status --json` prints when the key page is mapped exactly as root installed it."""
    hostport = "%s:%d" % (cfg["rp_id"], cfg["keyport"])
    doc = {"TCP": {str(cfg["keyport"]): {"HTTPS": True}},
           "Web": {hostport: {"Handlers": {"/": {"Path": cfg["page_dir"]},
                                           "/api": {"Proxy": "http://127.0.0.1:%d" % cfg["gateport"]}}}}}
    doc.update(over)
    return doc


def serve_ok(ctx, cfg, status=None, operator="", environ=b"PATH=/usr/sbin:/usr/bin\0PORT=41641\0"):
    """Make ctx.cmd (a FakeCmd) answer the reads of serve.problem, and ctx.proc hold tailscaled's process (its name
    and its environment, no command line, so the game check never matches it); status None = the installed
    mapping."""
    import json
    from zeus_root.ctx import CmdResult
    ctx.cmd.answers[SERVE_STATUS] = CmdResult(0, json.dumps(serve_status(cfg) if status is None else status), "")
    ctx.cmd.answers[SERVE_PREFS] = CmdResult(0, json.dumps({"OperatorUser": operator, "WantRunning": True}), "")
    ctx.cmd.answers[SERVE_PID] = CmdResult(0, TAILSCALED_PID + "\n", "")
    folder = ctx.proc / TAILSCALED_PID
    folder.mkdir(exist_ok=True)
    (folder / "comm").write_bytes(b"tailscaled\n")
    (folder / "environ").write_bytes(environ)


class Phone:
    """A passkey registered in ctx's keys.json for one action, on one device, that signs records the way the key page
    and Android will: root's challenge from the installed list and config, an ES256 signature over
    authenticatorData || SHA-256(clientDataJSON). counting=True makes each record's sign counter one higher."""

    def __init__(self, ctx, action="restart-bluetooth", counting=False, node_id="nTEST1CNTRL",
                 login="owner@example.com", register=True):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from zeus_root import canon, config
        self.ctx, self.counting, self.count = ctx, counting, 0
        self.priv = ec.generate_private_key(ec.SECP256R1())
        spki = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)
        cid = os.urandom(32)
        self.key = make_key(action=action, id=cid, ref=canon.key_ref(cid), spki=spki, node_id=node_id, login=login)
        if register:
            keys = config.load_keys(ctx)
            keys[self.key.ref] = self.key
            config.save_keys(ctx, keys)

    def record(self, action=None, nonce=None, exp=None, params=None, list_sha=None, count=None, flags=FLAGS_OK,
               bad_sig=False, seen=None, origin=None):
        import hashlib
        import json
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from zeus_root import canon, config
        cfg = config.load_config(self.ctx)
        if list_sha is None:
            list_sha = config.load_actions(self.ctx)[1]
        action = action or self.key.action
        params = ({"key": self.key.ref} if action == canon.CHECK_KEY else {}) if params is None else params
        nonce = nonce or canon.b64u_encode(os.urandom(32))
        exp = int(self.ctx.now()) + 120 if exp is None else exp
        ch = canon.challenge(action, params, list_sha, nonce, exp, origin or cfg.origin)
        client = json.dumps({"type": "webauthn.get", "challenge": canon.b64u_encode(ch),
                             "origin": origin or cfg.origin, "crossOrigin": False}, separators=(",", ":")).encode()
        if count is None:
            self.count += 1 if self.counting else 0
            count = self.count
        auth = hashlib.sha256(cfg.rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
        sig = self.priv.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        if bad_sig:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        return {"type": "check-key" if action == canon.CHECK_KEY else "run", "action": action, "params": params,
                "list": list_sha, "nonce": nonce, "exp": exp, "credentialId": canon.b64u_encode(self.key.id),
                "clientDataJSON": canon.b64u_encode(client), "authenticatorData": canon.b64u_encode(auth),
                "signature": canon.b64u_encode(sig), "userHandle": canon.b64u_encode(self.key.user),
                "seen": dict({"login": self.key.login, "node": "phone", "node_id": self.key.node_id,
                              "ip": "192.0.2.9"}, **(seen or {}))}

    def check_in(self, run, **seen):
        return {"type": "reach", "run": run,
                "seen": dict({"login": self.key.login, "node": "phone", "node_id": self.key.node_id,
                              "ip": "192.0.2.9"}, **seen)}
