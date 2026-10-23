"""The Throne plugin's Python side (plugin/plugin.py, docs/design.md §9): reads are file reads that change nothing, every
write runs zeus with fixed arguments (never a shell), and the plugin never decides. The page itself is tested in a browser
(tests/throne/throne.test.js, run under Elaia by the throne-page CI job)."""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

R = Path(__file__).resolve().parent.parent
SHA, PREV = "a" * 40, "b" * 40


def load_plugin():
    spec = importlib.util.spec_from_file_location("throne_plugin", R / "plugin" / "plugin.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Ctx:
    def __init__(self, headers=None):
        self.headers = headers or {}


def stamp(dt):
    return dt.isoformat(timespec="seconds")


class ThronePluginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        for d in ("queue/.done", "state", "log"):
            (self.data / d).mkdir(parents=True)
        cfg = self.tmp / "repos.json"
        cfg.write_text(json.dumps({"version": 1, "repos": {"you/argus-ui": {}, "you/hermes": {}}}), encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {"ZEUS_DATA": str(self.data), "ZEUS_CONFIG": str(cfg),
                                                "ZEUS_ALERT": str(self.tmp / "alert.json"), "ZEUS_BIN": "zeus-test"})
        self.env.start()
        self.p = load_plugin()
        self.now = datetime.now(timezone.utc).astimezone()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rec(self, rid, state="pending", folder="queue", **kw):
        r = {"id": rid, "action": "deploy", "state": state, "repo": "you/argus-ui", "sha": SHA, "previous": PREV,
             "subject": "Mentor: Learn phase", "version": "4.9.0", "checks": "success", "changelog": ["- Learn cards"],
             "files": 12, "rules_changed": [], "expires": stamp(self.now + timedelta(days=2)), "approval": None, "deploy": None}
        r.update(kw)
        path = self.data / folder / (rid + ".json")
        path.write_text(json.dumps(r), encoding="utf-8")
        return path

    # ---- reads ----
    def test_empty(self):
        code, s = self.p.get("", {}, Ctx())
        self.assertEqual(code, 200)
        self.assertEqual((s["waiting"], s["deploying"], s["history"], s["alert"]), ([], [], [], None))
        self.assertEqual([d["repo"] for d in s["deployed"]], ["you/argus-ui", "you/hermes"])
        self.assertEqual(s["line"], "Nothing waiting")

    def test_waiting_newest_first_and_expired_shown_without_writing(self):
        self.rec("20261021-090000-argus-ui-aaaaaaa")
        self.rec("20261022-090000-argus-ui-aaaaaab", state="held")
        old = self.rec("20261020-090000-argus-ui-aaaaaac", expires=stamp(self.now - timedelta(hours=1)))
        before = old.read_bytes()
        s = self.p.snapshot(self.now)
        self.assertEqual([r["id"] for r in s["waiting"]], ["20261022-090000-argus-ui-aaaaaab", "20261021-090000-argus-ui-aaaaaaa"])
        self.assertEqual([(r["id"], r["state"]) for r in s["history"]], [("20261020-090000-argus-ui-aaaaaac", "expired")])
        self.assertEqual(old.read_bytes(), before, "a GET never changes a record: Zeus applies expiry himself")
        self.assertTrue(s["line"].startswith("2 deploys waiting"))

    def test_broken_files_are_skipped(self):
        (self.data / "queue" / "20261021-090000-x-1234567.json").write_text("{nope", encoding="utf-8")
        self.rec("20261021-090001-argus-ui-aaaaaaa")
        self.assertEqual(len(self.p.snapshot(self.now)["waiting"]), 1)

    def test_deploying_stages_from_the_log(self):
        rid = "20261021-090000-argus-ui-aaaaaaa"
        self.rec(rid, state="deploying", folder="queue/.done")
        (self.data / "log" / ("deploy-%s.log" % rid)).write_text(
            "zeus: 2026-10-21T09:00:01 deploying you/argus-ui 4.9.0 (aaaaaaa)\nzeus: 2026-10-21T09:00:02 checked out aaaaaaa (detached) in ~/src/argus-ui\n"
            "zeus: 2026-10-21T09:00:03 running scripts/deploy.sh --no-pull\nbuilding...\n", encoding="utf-8")
        s = self.p.snapshot(self.now)
        self.assertEqual([(x["name"], x["state"]) for x in s["deploying"][0]["stages"]],
                         [("fetch", "done"), ("checkout", "done"), ("build", "now"), ("install", "todo"), ("health", "todo")])
        self.assertIn("building...", s["deploying"][0]["tail"])
        self.assertEqual(s["history"], [], "a running deploy is not history yet")

    def test_stages_when_it_ended(self):
        lines = ["zeus: t deploying", "zeus: t checked out", "zeus: t running scripts/deploy.sh --no-pull", "ZEUS_STAGE=install"]
        st = lambda state: [x["state"] for x in self.p.stages({"state": state}, lines)]
        self.assertEqual(st("deployed"), ["done"] * 5)
        self.assertEqual(st("rolled-back"), ["done", "done", "done", "failed", "todo"])
        self.assertEqual(st("approved")[0], "done")
        self.assertEqual([x["state"] for x in self.p.stages({"state": "approved"}, [])][0], "now", "approved, log not begun: fetch is next")

    def test_deployed_now_and_rollback_target(self):
        (self.data / "state" / "you--argus-ui.json").write_text(json.dumps({"repo": "you/argus-ui",
            "deployed": {"sha": SHA, "version": "4.9.0", "at": stamp(self.now), "approval": {"user": "owner@example.com", "via": "elaia"}},
            "previous": {"sha": PREV, "version": "4.8.0"}}), encoding="utf-8")
        s = self.p.snapshot(self.now)
        d = s["deployed"][0]
        self.assertEqual((d["deployed"]["version"], d["previous"]["version"]), ("4.9.0", "4.8.0"))
        self.assertIsNone(s["deployed"][1]["deployed"])
        self.assertEqual(s["line"], "Nothing waiting · argus-ui 4.9.0 live")

    def test_history_marks_logs_and_alert(self):
        rid = "20261021-090000-argus-ui-aaaaaaa"
        self.rec(rid, state="failed", folder="queue/.done", reason="build failed")
        (self.data / "log" / ("deploy-%s.log" % rid)).write_text("zeus: t deploying\n", encoding="utf-8")
        (self.tmp / "alert.json").write_text(json.dumps({"job": "zeus-deploy", "level": "warn", "message": "argus-ui 4.9.0 failed before the install"}), encoding="utf-8")
        s = self.p.snapshot(self.now)
        self.assertTrue(s["history"][0]["has_log"])
        self.assertEqual(s["alert"]["message"], "argus-ui 4.9.0 failed before the install")

    def test_home_status(self):
        self.assertEqual(self.p.get("status", {}, Ctx())[1], {"line": "Nothing waiting", "needs": False, "action": "", "count": 0})
        self.rec("20261021-090000-argus-ui-aaaaaaa")
        st = self.p.get("status", {}, Ctx())[1]
        self.assertEqual((st["needs"], st["action"], st["count"]), (True, "Review argus-ui 4.9.0", 1))
        self.rec("20261021-090001-hermes-aaaaaab", repo="you/hermes")
        self.assertEqual(self.p.get("status", {}, Ctx())[1]["action"], "Review 2 deploys")
        self.assertLessEqual(len(self.p.home_status()["action"]), 32)

    def test_log_endpoint(self):
        self.assertEqual(self.p.get("log", {"id": ["../../etc/passwd"]}, Ctx())[0], 400)
        rid = "20261021-090000-argus-ui-aaaaaaa"
        self.rec(rid, state="deployed", folder="queue/.done")
        (self.data / "log" / ("deploy-%s.log" % rid)).write_text("".join("line %d\n" % i for i in range(200)), encoding="utf-8")
        code, doc = self.p.get("log", {"id": [rid]}, Ctx())
        self.assertEqual((code, len(doc["lines"]), doc["lines"][-1], doc["state"]), (200, 80, "line 199", "deployed"))
        self.assertEqual(self.p.get("nope", {}, Ctx())[0], 404)

    # ---- writes: zeus with fixed arguments ----
    def call(self, path, body, headers=None, rc=0, out="ok", err=""):
        seen = {}
        def fake(argv, **kw):
            seen.update(argv=argv, kw=kw)
            return subprocess.CompletedProcess(argv, rc, out, err)
        with mock.patch.object(self.p.subprocess, "run", fake), mock.patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.4 5 6 7"}):
            code, doc = self.p.post(path, body, Ctx(headers))
        return code, doc, seen

    def test_approve_passes_who_is_asking_to_zeus(self):
        rid = "20261021-090000-argus-ui-aaaaaaa"
        code, doc, seen = self.call("approve", {"id": rid, "sha": SHA},
                                    {"Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "203.0.113.7, 127.0.0.1"})
        self.assertEqual(seen["argv"], ["zeus-test", "approve", rid, "--sha", SHA, "--user", "owner@example.com", "--ip", "203.0.113.7"])
        self.assertEqual((code, doc["ok"]), (200, True))
        self.assertNotIn("shell", seen["kw"])
        self.assertNotIn("SSH_CONNECTION", seen["kw"]["env"], "a Throne call must never look like an ssh session to Zeus")

    def test_approve_without_headers_still_asks_zeus(self):
        rid = "20261021-090000-argus-ui-aaaaaaa"
        code, doc, seen = self.call("approve", {"id": rid, "sha": SHA}, rc=1, err="zeus approve: no identity to check")
        self.assertEqual(seen["argv"], ["zeus-test", "approve", rid, "--sha", SHA], "the plugin doesn't judge: Zeus refuses")
        self.assertEqual((doc["ok"], doc["code"]), (False, 1))
        self.assertIn("no identity", doc["msg"])

    def test_bad_input_never_reaches_zeus(self):
        for path, body in (("approve", {"id": "x; rm -rf ~", "sha": SHA}), ("approve", {"id": "20261021-090000-a-b", "sha": "abc"}),
                           ("decline", {"id": "20261021-090000-a-b", "reason": "Not Now!"}), ("rollback", {"repo": "../x", "sha": SHA}),
                           ("recheck", {"repo": "argus-ui --file /etc/x"})):
            code, doc, seen = self.call(path, body)
            self.assertEqual(code, 400, path)
            self.assertEqual(seen, {}, path)
        self.assertEqual(self.call("deploy-everything", {})[0], 404)

    def test_decline_rollback_recheck_seen(self):
        rid = "20261021-090000-argus-ui-aaaaaaa"
        self.assertEqual(self.call("decline", {"id": rid, "reason": "not-now"})[2]["argv"][1:], ["decline", rid, "--reason", "not-now"])
        self.assertEqual(self.call("rollback", {"repo": "you/argus-ui", "sha": PREV}, {"Tailscale-User-Login": "owner@example.com"})[2]["argv"][1:],
                         ["rollback", "you/argus-ui", "--sha", PREV, "--user", "owner@example.com"])
        self.assertEqual(self.call("recheck", {"repo": "you/argus-ui"})[2]["argv"][1:], ["request", "you/argus-ui"])
        self.assertEqual(self.call("seen", {})[2]["argv"][1:], ["seen"])

    def test_zeus_missing_or_slow(self):
        def missing(argv, **kw):
            raise FileNotFoundError(argv[0])
        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, 120)
        for fake, code in ((missing, 127), (slow, 124)):
            with mock.patch.object(self.p.subprocess, "run", fake):
                doc = self.p.post("seen", {}, Ctx())[1]
            self.assertEqual((doc["ok"], doc["code"]), (False, code))

    def test_manifest(self):
        m = json.loads((R / "plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((m["id"], m["kind"]), ("throne", "server"))
        self.assertEqual(m["version"], (R / "VERSION").read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
