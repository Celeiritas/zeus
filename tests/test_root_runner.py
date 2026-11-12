"""zeus-root: the runner (root/zeus_root/runner.py, plan Task 7 with amendments v2-v6; Review Focus 1, 3 and 5).

One start of zeus-root-run.service: root's own files, the inbox taken, each record verified, at most one listed script
run with hold-or-undo-once. The tests drive runner.start on a Ctx under a temporary folder: the current user plays
root and the gate, the scripts are small sh files, Serve's state comes from a FakeCmd, games from a fake /proc. Linux
only (flock, scripts, file modes) except ParseTest.
"""
import contextlib
import hashlib
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from tests import root_support
from tests.root_support import Phone, good_tree, install_config, install_list, need, serve_ok, serve_status
from zeus_root import __version__, canon, cli, logs, runner, serve, state, take

BT = "Restart Bluetooth on {{SERVER}}"
CLOSE = "Close the media ports to the apartment network"
OK_SCRIPT = "#!/bin/sh\necho ran $ZEUS_ROOT_RUN\necho ZEUS_STAGE=change\nexit 0\n"
FAIL_AFTER = "#!/bin/sh\necho ZEUS_STAGE=change\necho broke\nexit 3\n"
UNDO_OK = "#!/bin/sh\necho undo $ZEUS_ROOT_RUN\necho ZEUS_STAGE=change\nexit 0\n"


def _plain_record(**over):
    """A run record with the right shape and nothing signed (for the strict parse alone)."""
    b = canon.b64u_encode
    rec = {"type": "run", "action": "restart-bluetooth", "params": {}, "list": "0" * 64, "nonce": b(bytes(32)),
           "exp": 1794000120, "credentialId": b(b"c" * 32), "clientDataJSON": b(b"{}"),
           "authenticatorData": b(bytes(37)), "signature": b(b"s" * 70), "userHandle": b(b"u" * 16),
           "seen": {"login": "owner@example.com", "node": "phone", "node_id": "nTEST1CNTRL", "ip": "192.0.2.9"}}
    rec.update(over)
    return rec


class ParseTest(unittest.TestCase):
    """The strict parse (D1, D3): anything but exactly a run, check-key or check-in record is Malformed (garbage)."""

    def ok(self, obj):
        return runner.parse_record(json.dumps(obj).encode())

    def bad(self, data):
        with self.assertRaises(canon.Malformed):
            runner.parse_record(data if isinstance(data, bytes) else json.dumps(data).encode())

    def test_good_shapes(self):
        self.assertEqual(self.ok(_plain_record())["action"], "restart-bluetooth")
        self.assertIsNone(self.ok(_plain_record(userHandle=None))["userHandle"])
        self.assertEqual(self.ok(_plain_record(type="check-key", action="check-key"))["type"], "check-key")
        reach = {"type": "reach", "run": "r" + "0" * 20, "seen": _plain_record()["seen"]}
        self.assertEqual(self.ok(reach)["run"], "r" + "0" * 20)

    def test_bad_shapes(self):
        rec, seen = _plain_record(), _plain_record()["seen"]
        cases = {
            "a list": [rec],
            "type unknown": dict(rec, type="enrol"),
            "a key missing": {k: v for k, v in rec.items() if k != "params"},
            "an extra key": dict(rec, extra=1),
            "exp a float": dict(rec, exp=1794000120.0),
            "exp a bool": dict(rec, exp=True),
            "exp a string": dict(rec, exp="1794000120"),
            "params a list": dict(rec, params=[]),
            "nonce short": dict(rec, nonce="A" * 42),
            "nonce with padding": dict(rec, nonce=canon.b64u_encode(bytes(32)) + "="),
            "nonce stray bits": dict(rec, nonce="A" * 42 + "B"),
            "action upper case": dict(rec, action="Restart"),
            "action a path": dict(rec, action="../x"),
            "list upper hex": dict(rec, list="A" * 64),
            "list short": dict(rec, list="0" * 63),
            "signature not base64url": dict(rec, signature="ab+/"),
            "credential id padded": dict(rec, credentialId=canon.b64u_encode(b"c" * 32) + "="),
            "user handle a number": dict(rec, userHandle=7),
            "seen extra": dict(rec, seen=dict(seen, name="x")),
            "seen short": dict(rec, seen={k: v for k, v in seen.items() if k != "node_id"}),
            "seen a number": dict(rec, seen=dict(seen, ip=1)),
            "reach bad run": {"type": "reach", "run": "u" + "0" * 20, "seen": seen},
            "reach extra": {"type": "reach", "run": "r" + "0" * 20, "seen": seen, "nonce": rec["nonce"]},
        }
        for what, obj in cases.items():
            with self.subTest(what):
                self.bad(obj)
        raw = json.dumps(rec)
        for what, data in (("repeated key", (raw[:-1] + ', "nonce": "%s"}' % rec["nonce"]).encode()),
                           ("not JSON", b"{"), ("empty", b""), ("NaN", raw.replace("1794000120", "NaN").encode())):
            with self.subTest(what):
                self.bad(data)


class RunnerCase(unittest.TestCase):
    def setUp(self):
        need("linux", "cryptography")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = root_support.make_ctx(tmp.name, script_limit_s=10, undo_limit_s=10, lock_wait_s=0.2)
        install_list(self.ctx)
        self.cfg = install_config(self.ctx)
        good_tree(self.ctx)
        serve_ok(self.ctx, self.cfg)
        self.t0 = self.ctx.now.t
        self.n = 0

    # --- setting up -------------------------------------------------------------------------------------------------
    def at(self, dt):
        self.ctx.now.t = self.t0 + dt

    def scripts(self, **bodies):
        """New script bodies (keyword = action id with _ for -) and their real sha256 in actions.json. Records made
        before this name the old list."""
        bodies = {k.replace("_", "-"): v for k, v in bodies.items()}
        path = self.ctx.etc / "actions.json"
        doc = json.loads(path.read_bytes())
        for e in doc["actions"]:
            if e["id"] in bodies:
                body = bodies[e["id"]].encode()
                script = self.ctx.libexec / "actions" / e["script"]
                script.write_bytes(body)
                os.chmod(script, 0o755)
                e["sha256"] = hashlib.sha256(body).hexdigest()
        path.write_bytes(json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8"))
        os.chmod(path, 0o644)

    def script_file(self, action):
        doc = json.loads((self.ctx.etc / "actions.json").read_bytes())
        return self.ctx.libexec / "actions" / next(e["script"] for e in doc["actions"] if e["id"] == action)

    def put(self, rec, folder="inbox"):
        self.n += 1
        data = rec if isinstance(rec, bytes) else json.dumps(rec).encode()
        path = self.ctx.run / folder / ("e%04d" % self.n)
        path.write_bytes(data)
        return path

    def game(self, pid=4242, cmdline=b"/usr/bin/gamescope\0-w\0001920\0--\0steam", comm=b"gamescope\n",
             exe="/usr/bin/gamescope"):
        d = self.ctx.proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(cmdline)
        (d / "comm").write_bytes(comm)
        os.symlink(exe, d / "exe")
        return d

    def start(self):
        self.assertEqual(runner.start(self.ctx), 0)

    @contextlib.contextmanager
    def lock_held(self):
        """Another start (or a put-back, a command) holds lib/lock: this start answers busy."""
        with state.lock(self.ctx):
            yield

    # --- reading back -----------------------------------------------------------------------------------------------
    def run_of(self, rec):
        return canon.run_id(rec["nonce"])

    def result(self, rec):
        return logs.read_result(self.ctx, self.run_of(rec))

    def lines(self, run=None):
        out = [obj for _, _, obj, _ in logs.iter_run_lines(self.ctx)]
        return out if run is None else [x for x in out if x.get("run") == run]

    def line(self, rec):
        got = self.lines(self.run_of(rec))
        self.assertEqual(len(got), 1, got)
        return got[0]

    def attention(self):
        path = self.ctx.log / "attention.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []

    def kinds(self):
        return [a["kind"] for a in self.attention()]

    def ran(self):
        """The runs whose script started: one log file each (run-<run>.log, an undo's run-u....log)."""
        return sorted(p.name[4:-4] for p in self.ctx.log.glob("run-*.log"))

    def script_log(self, run):
        return runner.log_path(self.ctx, run).read_text()

    def kept_until(self, nonce):
        """The spent nonce's file time (kept a day after it), or None when it is not spent."""
        path = state._nonce_path(self.ctx, nonce)
        return os.stat(path).st_mtime if path.exists() else None

    def folder(self, name):
        return sorted(os.listdir(self.ctx.run / name))

    def chain_ok(self):
        self.assertEqual(logs.chain_problems(self.ctx), [])
        for x in self.lines():
            self.assertEqual(x["zeus_root"], __version__)


class RunTest(RunnerCase):
    """One script run, hold or undo once (design §16.4 step 7, plan Task 7's exit mapping)."""

    def test_done(self):
        self.scripts(restart_bluetooth=OK_SCRIPT)
        phone = Phone(self.ctx)
        rec = phone.record()
        self.put(rec)
        self.start()
        run = self.run_of(rec)
        res = self.result(rec)
        self.assertEqual((res["state"], res["action"], res["sentence"], res["undo_ran"], res["class"]),
                         ("done", "restart-bluetooth", None, None, None))
        line = self.line(rec)
        self.assertEqual((line["outcome"], line["exit"], line["staged"], line["timed_out"], line["undo_ran"]),
                         ("done", 0, True, False, False))
        self.assertEqual(line["key"], {"ref": phone.key.ref, "label": "phone", "device": "phone",
                                       "synced_claimed": True})
        self.assertEqual(set(line["seen"]), {"login", "node", "ip"})
        self.assertEqual(set(line["assertion"]), {"clientDataJSON", "authenticatorData", "signature"})
        self.assertTrue(line["uv"])
        self.assertEqual(self.ran(), [run])
        self.assertIn("ran " + run, self.script_log(run))
        self.assertEqual((self.folder("inbox"), self.folder("work"), self.folder("quarantine")), ([], [], []))
        self.assertEqual(state.key_state(self.ctx, phone.key.ref), (0, rec["exp"]))
        self.assertEqual(state.limit_reason(self.ctx, runner.load(self.ctx).actions["restart-bluetooth"]), "min-gap")
        self.assertEqual(self.attention(), [])
        self.assertEqual(os.stat(self.ctx.run / "results" / (run + ".json")).st_mode & 0o777, 0o644)
        self.chain_ok()

    def test_stopped_before_stage_runs_no_undo(self):
        phone = Phone(self.ctx, action="close-media-lan")
        for i, code in enumerate((1, 2)):
            with self.subTest(exit=code):
                self.scripts(close_media_lan="#!/bin/sh\necho checking\nexit %d\n" % code)
                self.at(i * 1000)
                rec = phone.record()
                self.put(rec)
                self.start()
                res = self.result(rec)
                self.assertEqual((res["state"], res["sentence"], res["undo_ran"]),
                                 ("stopped", CLOSE + " stopped before it changed anything.", None))
                line = self.line(rec)
                self.assertEqual((line["outcome"], line["exit"], line["staged"], line["undo_ran"]),
                                 ("stopped", code, False, False))
                self.assertNotIn("undo", line)
                self.assertFalse(any(r.startswith("u") for r in self.ran()))
                self.assertFalse(list((self.ctx.lib / "reach").glob("*.json")))
        self.assertEqual(self.attention(), [])

    def test_failed_after_stage_runs_undo_once(self):
        self.scripts(close_media_lan=FAIL_AFTER, open_media_lan=UNDO_OK)
        rec = Phone(self.ctx, action="close-media-lan").record()
        self.put(rec)
        self.start()
        run = self.run_of(rec)
        urun = "u" + run[1:]
        res = self.result(rec)
        self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"], res["sentence"]),
                         ("failed", True, True, CLOSE + " failed after it changed something, and it was put back."))
        line = self.line(rec)
        self.assertEqual((line["outcome"], line["exit"], line["staged"], line["undo_ran"], line["undo_ok"]),
                         ("failed", 3, True, True, True))
        self.assertEqual(line["undo"], {"run": urun, "action": "open-media-lan", "exit": 0, "secs": line["undo"]["secs"],
                                        "timed_out": False})
        self.assertEqual(self.ran(), [run, urun])
        self.assertEqual(self.script_log(urun).count("undo " + urun), 1)
        self.assertFalse(list((self.ctx.lib / "reach").glob("*.json")))
        self.assertEqual(self.attention(), [])
        self.chain_ok()

    def test_undo_failure_writes_attention(self):
        for what, undo in (("undo exits 4", "#!/bin/sh\necho ZEUS_STAGE=change\nexit 4\n"),
                           ("undo script changed", None)):
            with self.subTest(what):
                self.setUp()
                self.scripts(close_media_lan=FAIL_AFTER, open_media_lan=undo or UNDO_OK)
                rec = Phone(self.ctx, action="close-media-lan").record()
                if undo is None:
                    with open(self.script_file("open-media-lan"), "a") as f:
                        f.write("# changed after the list was made\n")
                self.put(rec)
                self.start()
                res = self.result(rec)
                self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"], res["sentence"]),
                                 ("failed", undo is not None, False,
                                  CLOSE + " failed after it changed something, and putting it back failed too."))
                self.assertEqual([(a["kind"], a["run"], a["sentence"]) for a in self.attention()],
                                 [("undo-failed", self.run_of(rec), CLOSE + " failed, and its undo failed too")])
                self.assertEqual(len(self.ran()), 2 if undo is not None else 1)

    def test_undo_null_failure_is_not_back(self):
        """The v3 restart rule: an entry without an undo that fails after its stage says the service did not come back,
        and nothing more runs."""
        self.scripts(restart_bluetooth=FAIL_AFTER)
        rec = Phone(self.ctx).record()
        self.put(rec)
        self.start()
        res = self.result(rec)
        self.assertEqual((res["state"], res["undo_ran"], res["sentence"]),
                         ("failed", False, BT + ": the service did not come back up."))
        self.assertEqual([(a["kind"], a["sentence"]) for a in self.attention()],
                         [("not-back", BT + ": the service did not come back up")])
        self.assertEqual(self.ran(), [self.run_of(rec)])

    def test_a_stage_line_after_a_partial_line_still_undoes(self):
        """Review N5: a script that printed part of a line just before its stage line has still changed something; a
        line that only ends in ZEUS_STAGE=change counts. One that ends in more does not."""
        self.scripts(close_media_lan="#!/bin/sh\nprintf 'step 1 of 2... '\necho ZEUS_STAGE=change\nexit 3\n",
                     open_media_lan=UNDO_OK)
        rec = Phone(self.ctx, action="close-media-lan").record()
        self.put(rec)
        self.start()
        res = self.result(rec)
        self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"]), ("failed", True, True))
        self.assertTrue(self.line(rec)["staged"])
        self.at(200)
        self.scripts(close_media_lan="#!/bin/sh\necho ZEUS_STAGE=changed\nexit 3\n")
        rec = Phone(self.ctx, action="close-media-lan").record()
        self.put(rec)
        self.start()
        self.assertEqual((self.result(rec)["state"], self.line(rec)["staged"]), ("stopped", False))

    def test_an_undo_that_cannot_start_did_not_run(self):
        """Review N7: an undo that could not start (exit 127 from root, not from the script) did not run."""
        self.scripts(close_media_lan=FAIL_AFTER, open_media_lan=UNDO_OK)
        os.chmod(self.script_file("open-media-lan"), 0o644)      # the hash still matches; it cannot be started
        rec = Phone(self.ctx, action="close-media-lan").record()
        self.put(rec)
        self.start()
        res = self.result(rec)
        self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"]), ("failed", False, False))
        line = self.line(rec)
        self.assertEqual((line["undo_ran"], line["undo_ok"], line["undo"]["exit"]), (False, False, 127))
        self.assertEqual(self.kinds(), ["undo-failed"])
        self.scripts(open_media_lan="#!/bin/sh\nexit 127\n")    # a script's own 127: it ran
        self.at(200)
        rec = Phone(self.ctx, action="close-media-lan").record()
        self.put(rec)
        self.start()
        self.assertEqual((self.result(rec)["undo_ran"], self.result(rec)["undo_ok"]), (True, False))

    def test_timeout_then_undo_gets_its_own_limit(self):
        """Review Focus 3: the script times out after its stage and is killed with its whole group; the undo still gets
        its own limit, and the result says what happened, also when the undo times out too. A child the script
        started in the background dies with it (the whole process group is killed), so it never writes later."""
        slow = "#!/bin/sh\necho ZEUS_STAGE=change\n(sleep 2; echo orphan) &\nsleep 5\necho never\n"
        for what, undo, ok in (("undo in time", "#!/bin/sh\nsleep 0.6\necho undone\nexit 0\n", True),
                               ("undo slow too", "#!/bin/sh\nsleep 5\necho never\nexit 0\n", False)):
            with self.subTest(what):
                self.setUp()
                self.ctx.script_limit_s = self.ctx.undo_limit_s = 1
                self.scripts(close_media_lan=slow, open_media_lan=undo)
                rec = Phone(self.ctx, action="close-media-lan").record()
                self.put(rec)
                t = time.monotonic()
                self.start()
                self.assertLess(time.monotonic() - t, 4.5)
                run = self.run_of(rec)
                res = self.result(rec)
                self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"]), ("failed", True, ok))
                line = self.line(rec)
                self.assertEqual((line["outcome"], line["timed_out"], line["staged"], line["exit"]),
                                 ("failed", True, True, -9))
                self.assertEqual(line["undo"]["timed_out"], not ok)
                self.assertGreaterEqual(line["undo"]["secs"], 0.5)
                self.assertNotIn("never", self.script_log(run))
                if ok:
                    self.assertIn("undone", self.script_log("u" + run[1:]))
                else:
                    self.assertEqual(self.kinds(), ["undo-failed"])
        time.sleep(2.5)      # a background child left alive would have written "orphan" by now
        for p in self.ctx.log.glob("run-*.log"):
            self.assertNotIn("orphan", p.read_text())

    def test_env_is_exactly_two_vars(self):
        self.scripts(restart_bluetooth="#!/bin/sh\nenv\necho ZEUS_STAGE=change\n")
        rec = Phone(self.ctx).record()
        self.put(rec)
        with mock.patch.dict(os.environ, {"ZEUS_LEAK": "1", "TZ": "Pacific/Chatham", "OPENSSL_CONF": "/tmp/x"}):
            self.start()
        run = self.run_of(rec)
        env = dict(x.split("=", 1) for x in self.script_log(run).splitlines() if "=" in x and x != "ZEUS_STAGE=change")
        self.assertEqual(set(env) - {"PWD", "SHLVL", "_", "OLDPWD"}, {"PATH", "ZEUS_ROOT_RUN"})
        self.assertEqual((env["PATH"], env["ZEUS_ROOT_RUN"]), ("/usr/sbin:/usr/bin", run))
        self.assertEqual(self.result(rec)["state"], "done")

    def test_cwd_root_stdin_closed_no_arguments(self):
        self.scripts(restart_bluetooth="#!/bin/sh\npwd\nif read x; then echo READ; else echo EOF; fi\n"
                                       "echo ARGS:$#\necho ZEUS_STAGE=change\n")
        rec = Phone(self.ctx).record()
        self.put(rec)
        self.start()
        self.assertEqual(self.script_log(self.run_of(rec)).splitlines()[:3], ["/", "EOF", "ARGS:0"])

    def test_check_key_runs_nothing(self):
        phone = Phone(self.ctx)
        rec = phone.record(action=canon.CHECK_KEY)
        self.assertEqual((rec["type"], rec["params"]), ("check-key", {"key": phone.key.ref}))
        self.put(rec)
        self.start()
        res = self.result(rec)
        self.assertEqual((res["state"], res["action"], res["sentence"]),
                         ("done", "check-key", "This key checked out. Nothing ran."))
        self.assertEqual(self.line(rec)["outcome"], "key-checked")
        self.assertEqual(self.ran(), [])
        self.assertIsNone(state.limit_reason(self.ctx, runner.load(self.ctx).actions["restart-bluetooth"]))
        self.assertEqual(state.key_state(self.ctx, phone.key.ref), (0, rec["exp"]))
        self.assertEqual(self.attention(), [])


class RefuseTest(RunnerCase):
    """The runner's own refusals after the assertion (D10): each one line, a refused result, the attention rule."""

    def refused(self, rec, reason, cls="other"):
        res = self.result(rec)
        self.assertEqual((res["state"], res["class"]), ("refused", cls), res)
        line = self.line(rec)
        self.assertEqual((line["outcome"], line["reason"], line["class"]), ("refused", reason, cls))
        return res, line

    def test_script_hash_changed_refused(self):
        rec = Phone(self.ctx).record()
        with open(self.script_file("restart-bluetooth"), "a") as f:
            f.write("# one more line\n")
        self.put(rec)
        self.start()
        res, _ = self.refused(rec, "script-changed")
        self.assertEqual(res["sentence"], "The script of %s changed since the list was made. Nothing ran." % BT)
        self.assertEqual(self.kinds(), ["list-changed"])
        self.assertEqual(self.ran(), [])

    def test_game_running_refused_and_named(self):
        import pwd
        self.game(pid=2, cmdline=b"")                                    # a kernel thread: no command line
        self.game(pid=100, cmdline=b"/usr/lib/systemd/systemd\0--user", comm=b"systemd\n", exe="/usr/lib/systemd")
        self.game(pid=os.getpid(), cmdline=b"python3 gamescope", comm=b"python3\n", exe="/usr/bin/python3")
        self.game()
        rec = Phone(self.ctx).record()
        self.put(rec)
        self.start()
        res, line = self.refused(rec, "game")
        self.assertEqual(res["sentence"], "A game is running, so %s did not start. Nothing ran." % BT)
        self.assertEqual(line["process"], {"pid": 4242, "user": pwd.getpwuid(os.getuid()).pw_name,
                                           "comm": "gamescope", "exe": "/usr/bin/gamescope"})
        self.assertEqual(self.attention(), [])
        self.assertEqual(self.ran(), [])

    def test_unreadable_process_list_fails_closed(self):
        self.ctx.proc = self.ctx.proc.parent / "no-proc-here"
        rec = Phone(self.ctx).record()
        self.put(rec)
        with mock.patch.object(serve, "cert_uid_problem", return_value=None):    # tailscaled's /proc entry is gone too
            self.start()
        _, line = self.refused(rec, "game")
        self.assertEqual(line["process"]["pid"], 0)

    def test_no_game_check_for_an_entry_without_it(self):
        """self-test has game_check false: a game running does not stop it."""
        self.game()
        rec = Phone(self.ctx, action="self-test").record()
        self.put(rec)
        self.start()
        self.assertEqual(self.result(rec)["state"], "done")

    def test_paused_refused_no_attention(self):
        state.set_pause(self.ctx, None, "the owner paused root changes")
        rec = Phone(self.ctx).record()
        self.put(rec)
        self.start()
        res, _ = self.refused(rec, "paused")
        self.assertEqual(res["sentence"], "Root changes are paused for now. Nothing ran.")
        self.assertEqual((self.attention(), self.ran()), ([], []))

    def test_serve_reasons_refused_with_one_buzz(self):
        hostport = "%s:%d" % (self.cfg["rp_id"], self.cfg["keyport"])
        cases = (("serve-changed", dict(status=serve_status(self.cfg, AllowFunnel={hostport: True}))),
                 ("operator-set", dict(operator="someone")),
                 ("cert-uid-set", dict(environ=b"PATH=/usr/bin\0TS_PERMIT_CERT_UID=1000\0")))
        for i, (reason, how) in enumerate(cases):
            with self.subTest(reason):
                self.at(i * 1000)
                serve_ok(self.ctx, self.cfg, **how)
                rec = Phone(self.ctx).record()
                self.put(rec)
                self.start()
                self.refused(rec, reason)
                self.assertEqual(self.attention()[-1]["kind"], "serve-changed")
                self.assertEqual(self.attention()[-1]["run"], self.run_of(rec))
        self.assertEqual(len(self.attention()), 3)
        self.assertEqual(self.ran(), [])

    def test_rate_limit_refused_no_attention(self):
        phone = Phone(self.ctx)
        first = phone.record()
        self.put(first)
        self.start()
        self.at(60)
        rec = phone.record()
        self.put(rec)
        self.start()
        res, line = self.refused(rec, "rate-limit")
        self.assertEqual(line["limit"], "min-gap")
        self.assertEqual(res["sentence"], "%s ran too recently or too often today. Nothing ran." % BT)
        self.assertEqual(self.attention(), [])
        self.assertEqual(self.ran(), [self.run_of(first)])

    def test_not_listed(self):
        rec = Phone(self.ctx, action="gone-action").record()
        self.put(rec)
        self.start()
        res, _ = self.refused(rec, "not-listed")
        self.assertEqual(res["sentence"], "This action is not on the list. Nothing ran.")
        self.assertEqual((self.attention(), self.ran()), ([], []))

    def test_a_runner_refusal_then_the_same_record_is_used_nonce(self):
        """Plan v3: a record refused for any of the runner's reasons can never run when sent again as it was."""
        hostport = "%s:%d" % (self.cfg["rp_id"], self.cfg["keyport"])
        script = self.script_file("restart-bluetooth")
        body = script.read_bytes()

        def change_script(on):
            script.write_bytes(body + b"# changed\n" if on else body)

        def game(on):
            if on:
                self.game()
            else:
                for p in (self.ctx.proc / "4242").iterdir():
                    p.unlink()
                (self.ctx.proc / "4242").rmdir()
        conditions = {
            "paused": lambda on: state.set_pause(self.ctx, None, "owner") if on else state.clear_pause(self.ctx),
            "serve-changed": lambda on: serve_ok(self.ctx, self.cfg, status=None if not on else
                                                 serve_status(self.cfg, AllowFunnel={hostport: True})),
            "game": game,
            "script-changed": change_script,
        }
        phone = Phone(self.ctx)
        for i, (reason, condition) in enumerate(conditions.items()):
            with self.subTest(reason):
                self.at(i * 1000)
                rec = phone.record()
                condition(True)
                self.put(rec)
                self.start()
                self.assertEqual(self.line(rec)["reason"], reason)
                condition(False)
                self.put(rec)
                self.start()
                lines = self.lines(self.run_of(rec))
                self.assertEqual([x["reason"] for x in lines], [reason, "used-nonce"])
                self.assertEqual(self.result(rec)["state"], "refused")
                self.assertEqual(self.ran(), [])
        with self.subTest("rate-limit"):
            self.at(10000)
            state.note_run(self.ctx, "restart-bluetooth")
            rec = phone.record(exp=int(self.ctx.now()) + 180)
            self.put(rec)
            self.start()
            self.assertEqual(self.line(rec)["reason"], "rate-limit")
            self.at(10000 + 121)
            self.put(rec)
            self.start()
            self.assertEqual([x["reason"] for x in self.lines(self.run_of(rec))], ["rate-limit", "used-nonce"])
            self.assertEqual(self.ran(), [])

    def test_three_counted_refusals_pause_and_one_line(self):
        """D9: only the cause buzzes. Two counted refusals write refused-counted; the third writes `pause` instead of
        its own line; while paused a counted refusal writes none and a genuine record is refused paused."""
        phone = Phone(self.ctx)
        recs = [phone.record(bad_sig=True) for _ in range(4)]
        for i, rec in enumerate(recs[:3]):
            self.at(i * 10)
            self.put(rec)
            self.start()
            self.refused(rec, "signature", "counted")
        self.assertEqual(self.kinds(), ["refused-counted", "refused-counted", "pause"])
        self.assertEqual(self.attention()[0]["sentence"],
                         "Zeus refused a try. The signature did not check out. Nothing ran.")
        self.assertEqual(self.attention()[2]["sentence"], "Zeus refused 3 tries; root changes are paused for an hour")
        self.assertEqual(state.pause_until(self.ctx), self.t0 + 20 + 3600)
        self.at(30)
        good = phone.record()
        self.put(recs[3])
        self.put(good)
        self.start()
        self.refused(recs[3], "signature", "counted")
        self.refused(good, "paused")
        self.assertEqual(len(self.attention()), 3)
        self.assertEqual(self.ran(), [])

    def test_older_than_last_is_quiet(self):
        """Plan v6, item 13 through the runner: the gate held try A back while the owner pressed again (B); B ran.
        A then comes in still fresh: refused older-than-last, not counted, no buzz, nothing runs, with a counting
        passkey (no false "copied") and with one whose counter stays 0 (no second run)."""
        for i, counting in enumerate((True, False)):
            with self.subTest(counting=counting):
                self.at(i * 1000)
                phone = Phone(self.ctx, counting=counting)
                a = phone.record(exp=int(self.ctx.now()) + 100)
                b = phone.record(exp=int(self.ctx.now()) + 150)
                self.put(b)
                self.start()
                self.assertEqual(self.result(b)["state"], "done")
                self.put(a)
                self.start()
                res, _ = self.refused(a, "older-than-last", "not-counted")
                self.assertTrue(res["sentence"].startswith("A newer request signed with this key"))
                self.assertEqual(self.attention(), [])
                self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 0)
                self.assertIn(self.run_of(b), self.ran())
                self.assertNotIn(self.run_of(a), self.ran())
        self.assertEqual(len(self.ran()), 2)

    def test_root_files_broken_runs_nothing(self):
        """Design §16.3: a file rule broken (here config.json writable by everyone) or a file that does not load: every
        record is refused root-files with its nonce spent (no exp), one file-rules line an hour, nothing runs."""
        for what, breaks in (("mode", lambda: os.chmod(self.ctx.etc / "config.json", 0o666)),
                             ("does not load", lambda: (self.ctx.etc / "keys.json").write_bytes(b"{"))):
            with self.subTest(what):
                self.setUp()
                rec = Phone(self.ctx).record()
                breaks()
                self.put(rec)
                self.start()
                res, line = self.refused(rec, "root-files")
                self.assertTrue(line["why"])
                self.assertNotIn("uv", line)
                self.assertEqual(res["sentence"], "Root's own files break the ownership or mode rules. Nothing ran.")
                self.assertEqual(self.kinds(), ["file-rules"])
                self.assertAlmostEqual(self.kept_until(rec["nonce"]), self.t0, delta=1)
                self.assertEqual(self.ran(), [])
                self.at(60)
                self.put(rec)
                self.start()
                self.assertEqual([x["reason"] for x in self.lines(self.run_of(rec))], ["root-files"] * 2)
                self.assertEqual(self.result(rec)["sentence"], res["sentence"])
                self.assertEqual(self.kinds(), ["file-rules"])     # one line an hour (D8)
                self.assertEqual(self.ran(), [])


class ReplayTest(RunnerCase):
    def test_replay_after_done(self):
        """Review Focus 1: the same record twice. The second is used-nonce, not counted, no buzz, and the result
        still says done (never overwritten), with a passkey that counts (never copied-key) and one that does not."""
        for i, counting in enumerate((True, False)):
            with self.subTest(counting=counting):
                self.at(i * 1000)
                rec = Phone(self.ctx, counting=counting).record()
                self.put(rec)
                self.start()
                path = self.ctx.run / "results" / (self.run_of(rec) + ".json")
                before = path.read_bytes()
                self.put(rec)
                self.start()
                self.assertEqual([(x["outcome"], x.get("reason"), x.get("class")) for x in self.lines(self.run_of(rec))],
                                 [("done", None, None), ("refused", "used-nonce", "not-counted")])
                self.assertEqual(path.read_bytes(), before)
                self.assertEqual(self.attention(), [])
        self.assertEqual(len(self.ran()), 2)
        self.chain_ok()

    def test_two_valid_records_one_start(self):
        """Review Focus 5: at most one script per start; the second record is spent and refused busy, so it can never
        run later, and the unit's time limit never cuts a run in half."""
        first = Phone(self.ctx).record()
        second = Phone(self.ctx, action="restart-resolved").record()
        self.put(first)
        self.put(second)
        self.start()
        self.assertEqual(self.result(first)["state"], "done")
        res = self.result(second)
        self.assertEqual((res["state"], res["class"]), ("refused", "other"))
        self.assertEqual(res["sentence"], "Another root change was running, so this one did not start. Nothing ran; "
                                          "try again.")
        line = self.line(second)
        self.assertEqual((line["reason"], line["why"]), ("busy", "one script per start"))
        self.assertEqual(self.ran(), [self.run_of(first)])
        self.put(second)
        self.start()
        self.assertEqual(self.lines(self.run_of(second))[-1]["reason"], "used-nonce")
        self.assertEqual(self.ran(), [self.run_of(first)])


class InboxTest(RunnerCase):
    """What the start does before and around the records: leftovers, the flood, the file rules, garbage."""

    def test_leftover_in_work_is_quarantined_and_spent(self):
        rec = Phone(self.ctx).record()
        self.put(rec, folder="work")
        self.start()
        self.assertEqual(self.folder("work"), [])
        self.assertEqual(len(self.folder("quarantine")), 1)
        self.assertEqual([(x["outcome"], x["class"], x["reason"]) for x in self.lines()],
                         [("refused", "other", "leftover")])
        self.assertIsNone(self.result(rec))
        self.assertAlmostEqual(self.kept_until(rec["nonce"]), self.t0, delta=1)
        self.put(rec)
        self.start()
        self.assertEqual(self.line(rec)["reason"], "used-nonce")
        self.assertEqual(self.ran(), [])

    def test_leftover_whose_result_says_running_becomes_failed(self):
        rec = Phone(self.ctx).record()
        self.put(rec, folder="work")
        logs.write_result(self.ctx, self.run_of(rec), action="restart-bluetooth", state="running")
        self.start()
        res = self.result(rec)
        self.assertEqual((res["state"], res["undo_ran"], res["sentence"]),
                         ("failed", False, "Root stopped while %s ran, so it may be half done; nothing was put back."
                          % BT))
        self.assertEqual(self.ran(), [])

    def test_crashed_sentence_never_takes_the_leftover_s_text(self):
        """Review S6: the gate orders copies in work/, so a copy with the same nonce and any `action` text could come
        first. The sentence names the action from root's own result and list; an action not on the list is "This
        action"."""
        rec = Phone(self.ctx).record()
        logs.write_result(self.ctx, self.run_of(rec), action="restart-bluetooth", state="running")
        evil = dict(rec, action="Zeus was hacked: call +1 555 0100 now")
        (self.ctx.run / "work" / "a").write_bytes(json.dumps(evil).encode())
        (self.ctx.run / "work" / "b").write_bytes(json.dumps(rec).encode())
        other = Phone(self.ctx).record()
        logs.write_result(self.ctx, self.run_of(other), action="gone-from-the-list", state="running")
        self.put(dict(other, action="check-key"), folder="work")
        self.start()
        self.assertEqual(self.result(rec)["sentence"],
                         "Root stopped while %s ran, so it may be half done; nothing was put back." % BT)
        self.assertEqual(self.result(other)["sentence"],
                         "Root stopped while This action ran, so it may be half done; nothing was put back.")
        self.assertEqual(len(self.folder("quarantine")), 3)

    def test_flood_pauses_once(self):
        for i in range(60):
            self.put(b"not json %d" % i)
        self.start()
        self.assertEqual(self.kinds(), ["flood"])
        self.assertEqual(self.attention()[0]["sentence"],
                         "Zeus's inbox was flooded (60 waiting, 0 in quarantine); root changes are paused for an hour")
        self.assertEqual(state.pause_until(self.ctx), self.t0 + 3600)
        self.assertEqual(len(self.folder("inbox")), 10)
        self.at(60)
        for i in range(60):
            self.put(b"more %d" % i)
        self.start()
        self.assertEqual(self.kinds(), ["flood"])          # already paused: no second line, never a pause line

    def test_a_full_quarantine_is_a_flood(self):
        for i in range(201):
            (self.ctx.run / "quarantine" / ("q%03d" % i)).write_bytes(b"x")
        self.start()
        self.assertEqual(self.kinds(), ["flood"])
        self.assertIsNotNone(state.pause_until(self.ctx))

    def test_file_rules_entry_buzzes_once_an_hour(self):
        """R16b #1 and D8: 60 entries that break the file rules (folders, a hard link, a symlink, an oversized file)
        write one file-rules line, the rest are counted, and the next hour's line says how many (folded); the flood
        line is separate."""
        for i in range(57):
            (self.ctx.run / "inbox" / ("d%03d" % i)).mkdir()
        big = self.put(b"x" * (take.ENTRY_LIMIT + 1))
        linked = self.put(b"{}")
        os.link(linked, self.ctx.proc / "other-name")
        os.symlink("/etc/hostname", self.ctx.run / "inbox" / "zlink")
        self.start()
        self.assertEqual(self.kinds(), ["flood", "file-rules"])
        self.assertEqual(self.attention()[1]["folded"], 0)

        def file_rules():
            return [x for x in self.lines() if x.get("reason") == "file-rules"]
        self.assertEqual(len(file_rules()), 50)
        self.at(3601)
        self.start()
        self.assertEqual(self.kinds(), ["flood", "file-rules", "file-rules"])
        self.assertEqual(self.attention()[2]["folded"], 49)
        self.assertEqual(len(file_rules()), 60)
        self.assertTrue(all(x["class"] == "other" and x["run"] is None for x in file_rules()))
        self.assertEqual({(x["entry"]["type"], x["why"]) for x in file_rules()},
                         {("folder", "not a regular file"), ("file", "too big"), ("file", "a hard link"),
                          ("link", "a link")})
        self.assertEqual((self.folder("inbox"), len(self.folder("quarantine"))), ([], 60))
        self.assertFalse(big.exists())
        self.assertIn(take.ENTRY_LIMIT + 1, [x["entry"]["size"] for x in file_rules()])
        self.assertEqual(self.ran(), [])

    def test_extra_or_repeated_keys_are_garbage(self):
        """Quarantined by the strict parse: garbage, never counted toward the pause, no result, no nonce spent (so the
        intact record still runs)."""
        rec = Phone(self.ctx).record()
        raw = json.dumps(rec)
        self.put(dict(rec, extra=1))
        self.put((raw[:-1] + ', "nonce": "%s"}' % rec["nonce"]).encode())
        self.start()
        self.assertEqual([(x["reason"], x["class"]) for x in self.lines()], [("malformed", "garbage")] * 2)
        self.assertEqual(len(self.folder("quarantine")), 2)
        self.assertIsNone(self.result(rec))
        self.assertIsNone(self.kept_until(rec["nonce"]))
        self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 0)
        self.assertIsNone(state.pause_until(self.ctx))
        self.assertEqual(self.attention(), [])
        self.put(rec)
        self.start()
        self.assertEqual(self.result(rec)["state"], "done")

    def test_a_nonce_of_the_wrong_shape_gets_no_result(self):
        rec = Phone(self.ctx).record()
        self.put(dict(rec, nonce="A" * 42))
        self.start()
        self.assertEqual(self.folder("results"), [])
        self.assertEqual([x for x in os.listdir(self.ctx.lib / "nonces") if not x.startswith(".")], [])
        self.assertEqual(self.lines()[0]["reason"], "malformed")

    def test_garbage_never_pauses(self):
        """25 garbage records in an hour (bad JSON, extra keys, an unknown key, another device): no pause, no buzz."""
        phone = Phone(self.ctx)
        stranger = Phone(self.ctx, register=False)
        for i in range(25):
            kind = i % 4
            self.put(b"{" if kind == 0 else dict(phone.record(), extra=1) if kind == 1 else
                     stranger.record() if kind == 2 else phone.record(seen={"node_id": "nOTHERDEVICE"}))
        self.start()
        self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 0)
        self.assertIsNone(state.pause_until(self.ctx))
        self.assertEqual(self.attention(), [])
        reasons = sorted({x["reason"] for x in self.lines()})
        self.assertEqual(reasons, ["malformed", "unknown-key", "wrong-device"])
        self.assertEqual(self.ran(), [])

    def test_open_cap(self):
        """Review M1: every start together opens at most 50 inbox entries an hour (each answer costs a log line and
        maybe a nonce file); past that, entries wait in quarantine unread, one line per start, nothing spent or
        answered. The hour after, records are opened again."""
        for i in range(50):
            self.put(b"{%d" % i)
        self.start()
        self.assertEqual(len(self.lines()), 50)
        self.at(10)
        rec = Phone(self.ctx).record()
        for i in range(3):
            self.put(b"[%d" % i)
        self.put(rec)
        self.start()
        last = self.lines()[-1]
        self.assertEqual((last["run"], last["outcome"], last["class"], last["reason"], last["count"]),
                         (None, "refused", "garbage", "open-cap", 4))
        self.assertEqual(len(self.lines()), 51)
        self.assertEqual(len(self.folder("quarantine")), 54)
        self.assertIsNone(self.result(rec))
        self.assertIsNone(self.kept_until(rec["nonce"]))
        self.assertEqual((self.attention(), self.ran()), ([], []))
        self.at(3601)
        rec = Phone(self.ctx).record()
        self.put(rec)
        self.start()
        self.assertEqual(self.result(rec)["state"], "done")


class CapTest(RunnerCase):
    """Review M1: the hourly cap counts every entry opened, whatever its answer, in every start (a busy one too), and
    a busy start leaves a flood to the start that holds lib/lock."""

    def caps(self):
        return [x["count"] for x in self.lines() if x.get("reason") == "open-cap"]

    def nonce_files(self):
        return len([x for x in os.listdir(self.ctx.lib / "nonces") if not x.startswith(".")])

    def test_every_kind_is_capped(self):
        """Bad signatures (counted), replays (used-nonce, not counted) and malformed entries (garbage): 60 in an hour
        open 50, and 10 go to quarantine unopened with one open-cap line."""
        phone = Phone(self.ctx)
        replayed = phone.record()
        kinds = {"bad signature": lambda i: phone.record(bad_sig=True), "replay": lambda i: replayed,
                 "malformed": lambda i: b"{%d" % i}
        for k, (what, make) in enumerate(kinds.items()):
            with self.subTest(what):
                self.at(k * 4000)
                if what == "replay":
                    replayed = phone.record()
                before, nonces = len(self.lines()), self.nonce_files()
                for batch in (40, 20):
                    for i in range(batch):
                        self.put(make(i))
                    self.start()
                new = self.lines()[before:]
                self.assertEqual(len([x for x in new if x.get("reason") != "open-cap"]), 50)
                self.assertEqual([(x["class"], x["count"]) for x in new if x.get("reason") == "open-cap"],
                                 [("garbage", 10)])
                self.assertEqual((self.folder("inbox"), self.folder("work")), ([], []))
                self.assertEqual(self.nonce_files() - nonces,
                                 {"bad signature": 50, "replay": 1, "malformed": 0}[what])

    def test_busy_start_caps_records_too(self):
        phone = Phone(self.ctx)
        for batch in (40, 20):
            for _ in range(batch):
                self.put(phone.record(bad_sig=True))
            with self.lock_held():
                self.start()
        self.assertEqual(len([x for x in self.lines() if x.get("reason") == "busy"]), 50)
        self.assertEqual(self.caps(), [10])
        self.assertEqual(self.nonce_files(), 50)
        self.assertEqual(len(self.folder("quarantine")), 10)

    def test_busy_and_locked_starts_share_the_cap(self):
        for i in range(30):
            self.put(b"{%d" % i)
        self.start()
        self.at(10)
        for i in range(30):
            self.put(b"[%d" % i)
        with self.lock_held():
            self.start()
        self.assertEqual(len([x for x in self.lines() if x.get("reason") == "malformed"]), 50)
        self.assertEqual(self.caps(), [10])
        self.at(20)
        self.put(b"{")
        self.start()
        self.assertEqual(self.caps(), [10, 1])

    def test_busy_start_leaves_a_flood_to_the_locked_start(self):
        for i in range(60):
            self.put(b"{%d" % i)
        with self.lock_held():
            self.start()
        self.assertEqual((len(self.folder("inbox")), self.folder("work"), self.lines(), self.kinds()),
                         (60, [], [], []))
        self.start()
        self.assertEqual(self.kinds(), ["flood"])
        self.assertIsNotNone(state.pause_until(self.ctx))
        self.assertEqual(len(self.folder("inbox")), 10)

    def test_busy_start_leaves_a_full_quarantine_to_the_locked_start(self):
        for i in range(201):
            (self.ctx.run / "quarantine" / ("q%03d" % i)).write_bytes(b"x")
        rec = Phone(self.ctx).record()
        self.put(rec)
        with self.lock_held():
            self.start()
        self.assertEqual((len(self.folder("inbox")), self.lines()), (1, []))
        self.start()
        self.assertEqual(self.kinds(), ["flood"])
        self.assertEqual((self.result(rec)["state"], self.line(rec)["reason"]), ("refused", "paused"))

    def test_flood_under_the_owner_s_pause_writes_its_line(self):
        """Review N2: the owner's pause hides no flood. One line for a flood that lasts (another within the hour
        writes none); a flood after an hour without one writes its line again."""
        state.set_pause(self.ctx, None, "the owner paused root changes")
        for h in range(3):
            for dt in (0, 60):
                self.at(h * 4000 + dt)
                for i in range(60):
                    self.put(b"x%d-%d-%d" % (h, dt, i))
                self.start()
        self.assertEqual(self.kinds(), ["flood"] * 3)
        self.assertTrue(self.attention()[0]["sentence"].endswith("root changes stay paused until you resume them"))
        self.assertEqual(state.pause_until(self.ctx), "owner")

    def test_flood_during_a_counted_pause_writes_its_line(self):
        state.set_pause(self.ctx, self.t0 + 3600, "3 counted refusals within an hour")
        for i in range(60):
            self.put(b"{%d" % i)
        self.start()
        self.assertEqual(self.kinds(), ["flood"])
        self.assertTrue(self.attention()[0]["sentence"].endswith("root changes are paused for an hour"))


class NonceTest(RunnerCase):
    """D10 as amended (v3-v6): every answer spends the nonce first; busy is checked up to the signature; a spend that
    fails writes no result and is never read as busy."""

    def test_busy_spends_nonce(self):
        rec = Phone(self.ctx).record()
        self.put(rec)
        with self.lock_held():
            self.start()
        res = self.result(rec)
        self.assertEqual((res["state"], res["class"]), ("refused", "other"))
        line = self.line(rec)
        self.assertEqual((line["reason"], line["signed"]), ("busy", True))
        self.assertAlmostEqual(self.kept_until(rec["nonce"]), rec["exp"], delta=1)
        self.put(rec)
        self.start()
        self.assertEqual(self.lines(self.run_of(rec))[-1]["reason"], "used-nonce")
        self.assertEqual(self.result(rec)["state"], "refused")
        self.assertEqual(self.ran(), [])

    def test_busy_good_signature_far_exp_is_used_nonce_near_that_exp(self):
        """Plan v6, the busy choice: the signature checks out, so the nonce is kept until a day after the signed exp;
        the intact record sent again when its exp comes near is used-nonce."""
        rec = Phone(self.ctx).record(exp=int(self.t0) + 2 * 86400)
        self.put(rec)
        with self.lock_held():
            self.start()
        self.assertAlmostEqual(self.kept_until(rec["nonce"]), rec["exp"], delta=1)
        self.at(2 * 86400 - 100)
        self.put(rec)
        self.start()
        self.assertEqual([x["reason"] for x in self.lines(self.run_of(rec))], ["busy", "used-nonce"])
        self.assertEqual(self.ran(), [])

    def test_busy_bad_signature_keeps_its_nonce_a_day(self):
        rec = Phone(self.ctx).record(exp=int(self.t0) + 2 * 86400, bad_sig=True)
        self.put(rec)
        with self.lock_held():
            self.start()
        self.assertFalse(self.line(rec)["signed"])
        self.assertAlmostEqual(self.kept_until(rec["nonce"]), self.t0, delta=1)
        self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 0)     # a busy start counts nothing
        self.at(86401)
        self.start()
        self.assertIsNone(self.kept_until(rec["nonce"]))

    def test_busy_start_still_quarantines_and_refuses_check_ins(self):
        phone = Phone(self.ctx)
        self.put(b"{")
        check_in = phone.check_in("r" + "1" * 20)
        self.put(check_in)
        with self.lock_held():
            self.start()
        self.assertEqual([(x["reason"], x["class"]) for x in self.lines()],
                         [("malformed", "garbage"), ("busy", "other")])
        self.assertEqual(self.lines()[1]["run"], "r" + "1" * 20)
        self.assertEqual((self.folder("inbox"), self.folder("work"), len(self.folder("quarantine"))), ([], [], 1))
        self.assertEqual(state.refusals_in_hour(self.ctx, "garbage"), 0)

    def test_nonce_busy_from_spend_writes_no_result(self):
        """Plan v6 (F2): a NonceBusy from the spend is not the runner's busy: no result, no busy line, the entry stays
        in work/ and the next start quarantines it as a leftover and spends it then."""
        rec = Phone(self.ctx).record()
        self.put(rec)
        with mock.patch.object(state, "spend_nonce", side_effect=state.NonceBusy("held")):
            self.start()
        self.assertIsNone(self.result(rec))
        line = self.line(rec)
        self.assertEqual((line["reason"], line["class"], line["nonce_store"]), ("nonce-store", "other", "NonceBusy"))
        self.assertEqual(len(self.folder("work")), 1)
        self.assertEqual(self.ran(), [])
        self.start()
        self.assertEqual(self.folder("work"), [])
        self.assertIsNotNone(self.kept_until(rec["nonce"]))
        self.put(rec)
        self.start()
        self.assertEqual(self.lines(self.run_of(rec))[-1]["reason"], "used-nonce")
        self.assertEqual(self.ran(), [])

    def test_spend_error_with_a_bad_signature_still_counts(self):
        """Plan v4: a forgery handed in while the disk is full is logged as `signature` and counts toward the pause."""
        rec = Phone(self.ctx).record(bad_sig=True)
        self.put(rec)
        with mock.patch.object(state, "spend_nonce", side_effect=OSError(28, "No space left on device")):
            self.start()
        self.assertIsNone(self.result(rec))
        line = self.line(rec)
        self.assertEqual((line["reason"], line["class"], line["nonce_store"]), ("signature", "counted", "OSError"))
        self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 1)
        self.assertEqual(self.kinds(), ["refused-counted"])

    def test_a_refused_result_comes_after_its_spend(self):
        """Plan v3: a `refused` result is written only after its nonce is spent (a refusal, Malformed from verify)."""
        phone = Phone(self.ctx)
        rec = phone.record(bad_sig=True)
        auth = canon.b64u_decode(rec["authenticatorData"])
        short = phone.record()
        short["authenticatorData"] = canon.b64u_encode(auth[:-1])    # passes the parse, Malformed inside verify
        real, seen = state.spend_nonce, []

        def spy(ctx, nonce, exp=None):
            seen.append(logs.read_result(ctx, canon.run_id(nonce)))
            return real(ctx, nonce, exp)
        self.put(rec)
        self.put(short)
        with mock.patch.object(state, "spend_nonce", spy):
            self.start()
        self.assertEqual(seen, [None, None])
        self.assertEqual(self.result(rec)["class"], "counted")
        res = self.result(short)
        self.assertEqual((res["state"], res["class"]), ("refused", "garbage"))
        self.assertEqual(self.line(short)["reason"], "malformed")
        self.assertAlmostEqual(self.kept_until(short["nonce"]), self.t0, delta=1)


class CliTest(RunnerCase):
    def test_usage_and_version(self):
        import io
        for argv in ([], ["help"], ["run", "x"], ["boot", "x"], ["reach-tick"], ["reach-tick", "../x"],
                     ["reach-tick", "u" + "0" * 20], ["version", "x"]):
            with self.subTest(argv=argv), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(cli.main(argv, self.ctx), 2)
        with mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(cli.main(["version"]), 0)
        self.assertEqual(out.getvalue().strip(), __version__)

    def test_main_refuses_without_root(self):
        import io
        with mock.patch.object(cli, "_clear_environment") as clear, \
                mock.patch.object(cli.os, "geteuid", return_value=1000), mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(cli.main(["run"]), 1)
        clear.assert_called_once_with()
        self.assertIn("run this as root", err.getvalue())

    def test_main_with_a_ctx_keeps_the_environment_and_runs(self):
        rec = Phone(self.ctx).record()
        self.put(rec)
        with mock.patch.object(cli, "_clear_environment") as clear:
            self.assertEqual(cli.main(["run"], self.ctx), 0)
        clear.assert_not_called()
        self.assertEqual(self.result(rec)["state"], "done")

    def test_a_start_that_fails_exits_1_without_quoting_a_record(self):
        import io
        (self.ctx.lib / "pause.json").write_bytes(b'{"until":')
        rec = Phone(self.ctx).record()
        self.put(rec)
        with mock.patch("sys.stderr", io.StringIO()) as err:
            self.assertEqual(cli.main(["run"], self.ctx), 1)
        self.assertIn("run failed: Broken", err.getvalue())
        self.assertNotIn(rec["nonce"], err.getvalue())
        self.assertEqual(len(self.folder("work")), 1)        # waits for the next start

    def test_clear_environment_keeps_path_only(self):
        with mock.patch.dict(os.environ, {"TZ": "Pacific/Chatham", "OPENSSL_CONF": "/tmp/x"}, clear=False):
            saved = dict(os.environ)
            try:
                cli._clear_environment()
                self.assertEqual(dict(os.environ), {"PATH": "/usr/sbin:/usr/bin"})
            finally:
                os.environ.clear()
                os.environ.update(saved)


class GamesTest(RunnerCase):
    def test_match_rules(self):
        from zeus_root import games
        self.assertIsNone(games.running(self.ctx, ["gamescope"]))
        self.game(pid=300, cmdline=b"/opt/games/SteamLibrary/x.exe\0", comm=b"x.exe\n", exe="/x")
        self.game(pid=200, cmdline=b"/usr/bin/GameScope\0", comm=b"gamescope\n", exe="/usr/bin/gamescope")
        self.assertEqual(games.running(self.ctx, ["gamescope", "steam"]).pid, 200)     # the lowest pid first
        self.assertEqual(games.running(self.ctx, ["STEAM"]).pid, 300)                  # case-insensitive substring
        self.assertIsNone(games.running(self.ctx, []))
        self.assertIsNone(games.running(self.ctx, [""]))
        self.assertIsNone(games.running(self.ctx, ["retroarch"]))

    def test_a_home_path_is_logged_from_tilde(self):
        """Review S5: the run log is readable by everyone, and Steam runs from the home folder."""
        from zeus_root import games
        steam = "/var/home/user/.local/share/Steam/ubuntu12_32/steam"
        self.game(pid=4242, cmdline=steam.encode() + b"\0-silent\0", comm=b"steam\n", exe=steam)
        self.assertEqual(games.running(self.ctx, ["steam"]).exe, "~/.local/share/Steam/ubuntu12_32/steam")
        cases = {"/home/user/bin/x": "~/bin/x", "/home/user": "~", "/var/home/you/b": "~/b",
                 "/homeless/x": "/homeless/x", "/home/": "/home/", "/usr/bin/gamescope": "/usr/bin/gamescope",
                 "/opt/home/user/y": "/opt/home/user/y", "": ""}
        for path, logged in cases.items():
            with self.subTest(path=path):
                self.assertEqual(games.home_as_tilde(path), logged)


if __name__ == "__main__":
    unittest.main()
