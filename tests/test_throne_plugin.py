"""The Throne plugin's Python side (plugin/plugin.py, docs/design.md §9): reads are file reads that change nothing, every
write runs zeus with fixed arguments (never a shell), and the plugin never decides. The page itself is tested in a browser
(tests/throne/throne.test.js, run under Elaia by the throne-page CI job)."""
import http.server
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
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
        self.assertEqual(s["line"], "Nothing waiting · argus-ui 4.9.0 deployed", "no health address answers: Zeus's record, never \"live\"")

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

    # ---- the decision inbox (0.5.0, docs/design.md §14) ----
    def ask(self, aid, state="open", **kw):
        r = {"id": aid, "kind": "decision", "tier": 3, "state": state, "project": aid.split("-", 3)[3],
             "title": "Tiers as proposed?", "options": ["Yes", "No"], "context": "", "asked": stamp(self.now - timedelta(hours=1)),
             "expires": stamp(self.now + timedelta(days=2)), "by": {"user": "owner", "via": "ssh"}, "answer": None, "sig": "0" * 64}
        r.update(kw)
        path = self.data / "queue" / (aid + ".json")
        path.write_text(json.dumps(r), encoding="utf-8")
        return path

    def test_asks_open_oldest_first_and_finished_apart(self):
        self.ask("ask-20261109-080000-zeus")
        self.ask("ask-20261109-070000-hub")
        old = self.ask("ask-20261101-070000-spartamelee", expires=stamp(self.now - timedelta(hours=1)))
        self.ask("ask-20261108-070000-mnemosyne", state="answered",
                 answer={"option": "No", "index": 2, "at": stamp(self.now), "by": {"user": "owner@example.com", "via": "elaia"}})
        (self.data / "queue" / "ask-20261109-090000-broken.json").write_text("{nope", encoding="utf-8")
        self.ask("ask-20261109-100000-liar", id="ask-20261109-100000-other")      # a record must carry its own file's id
        self.rec("20261021-090000-argus-ui-aaaaaaa")
        before = old.read_bytes()
        s = self.p.snapshot(self.now)
        self.assertEqual([a["id"] for a in s["asks"]], ["ask-20261109-070000-hub", "ask-20261109-080000-zeus"],
                         "open asks, the one waiting longest on top")
        self.assertEqual([(a["id"], a["state"]) for a in s["asked"]],
                         [("ask-20261108-070000-mnemosyne", "answered"), ("ask-20261101-070000-spartamelee", "expired")])
        self.assertEqual(s["asked"][0]["answer"]["option"], "No")
        self.assertEqual(old.read_bytes(), before, "a GET never changes an ask: Zeus records the expiry himself")
        self.assertEqual([r["id"] for r in s["waiting"]], ["20261021-090000-argus-ui-aaaaaaa"], "asks are never deploy cards")
        self.assertEqual([r["id"] for r in s["history"]], [])
        self.assertEqual(s["line"], "2 questions for you · 1 deploy waiting")

    def test_an_ask_reaches_the_page_as_data_only(self):
        hostile = '<img src=x onerror="alert(1)">"</script>'
        self.ask("ask-20261109-080000-zeus", title=hostile, options=["<b>yes</b>", 'no & "later"'], context="a\n<script>",
                 command="rm -rf ~", repo="you/argus-ui", sha=SHA)
        a = self.p.snapshot(self.now)["asks"][0]
        self.assertEqual(sorted(a), ["answer", "asked", "context", "expires", "id", "options", "project", "state", "title"],
                         "only the fields the page shows; nothing an ask carries can look like a deploy")
        self.assertEqual((a["title"], a["options"], a["context"]), (hostile, ["<b>yes</b>", 'no & "later"'], "a\n<script>"))
        code, doc = self.p.get("", {}, Ctx())
        self.assertEqual(json.loads(json.dumps(doc))["asks"][0]["title"], hostile, "the page gets the text as it was asked")

    def test_home_status_and_summary_count_asks(self):
        self.assertEqual(self.p.get("summary", {}, Ctx()), (200, {"asks": 0, "deploys": 0, "needs_you": 0, "line": "Nothing waiting", "stale": 0}))
        self.ask("ask-20261109-080000-zeus")
        self.assertEqual(self.p.get("status", {}, Ctx())[1], {"line": "1 question for you · no deploys waiting", "needs": True,
                                                              "action": "Answer 1 question", "count": 1})
        self.ask("ask-20261109-070000-hub")
        self.rec("20261021-090000-argus-ui-aaaaaaa")
        st = self.p.get("status", {}, Ctx())[1]
        self.assertEqual((st["needs"], st["action"], st["count"]), (True, "Answer 2, review 1", 3))
        self.assertEqual(self.p.get("summary", {}, Ctx())[1], {"asks": 2, "deploys": 1, "needs_you": 3,
                                                               "line": "2 questions for you · 1 deploy waiting", "stale": 0})

    def test_answer_runs_zeus_with_the_option_number_and_who(self):
        aid = "ask-20261109-080000-zeus"
        code, doc, seen = self.call("answer", {"id": aid, "index": 2},
                                    {"Tailscale-User-Login": "owner@example.com", "X-Forwarded-For": "203.0.113.7"})
        self.assertEqual(seen["argv"], ["zeus-test", "answer", aid, "--index", "2", "--user", "owner@example.com", "--ip", "203.0.113.7"])
        self.assertEqual((code, doc["ok"]), (200, True))
        code, doc, seen = self.call("answer", {"id": aid, "index": 1}, {"Tailscale-User-Login": "owner@example.com",
                                    "X-Forwarded-For": "203.0.113.7"}, rc=1, err="zeus answer: %s was already answered" % aid)
        self.assertEqual((doc["ok"], doc["code"]), (False, 1), "Zeus refuses a second answer; the plugin shows it")
        for headers in (None, {}, {"X-Forwarded-For": "203.0.113.7"}, {"Tailscale-User-Login": "  "}):
            code, doc, seen = self.call("answer", {"id": aid, "index": 1}, headers)
            self.assertEqual((code, doc["ok"], seen), (403, False, {}), "a tap with no tailnet login never reaches Zeus")
            self.assertIn("no tailnet login", doc["msg"])
        for body in ({"id": aid, "index": 0}, {"id": aid, "index": 9}, {"id": aid, "index": "1"}, {"id": aid, "index": True},
                     {"id": aid}, {"id": "20261021-090000-argus-ui-aaaaaaa", "index": 1}, {"id": "ask-../../x", "index": 1},
                     {"id": aid + " --index 1", "index": 1}):
            code, doc, seen = self.call("answer", body)
            self.assertEqual((code, seen), (400, {}), body)

    # ---- what really runs (0.7.0): running, drift, stale requests ----
    def health_server(self, answer):
        """A tiny HTTP server on a free port of 127.0.0.1 for this test only. answer(path) -> (status, headers, body) or
        a number of seconds to sleep before answering. Returns (port, list of paths asked)."""
        asked = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                asked.append(self.path)
                a = answer(self.path)
                if isinstance(a, (int, float)):
                    time.sleep(a)
                    a = (200, {}, b'{"version": "9.9.9"}')
                code, headers, body = a
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1], asked

    def config(self, health):
        repos = {"you/argus-ui": {"health": health} if health else {}, "you/hermes": {}}
        Path(os.environ["ZEUS_CONFIG"]).write_text(json.dumps({"version": 1, "repos": repos}), encoding="utf-8")

    def state(self, version, prev="4.32.0"):
        (self.data / "state" / "you--argus-ui.json").write_text(json.dumps({"repo": "you/argus-ui",
            "deployed": {"sha": SHA, "version": version, "at": stamp(self.now)}, "previous": {"sha": PREV, "version": prev}}), encoding="utf-8")

    def runs(self, version=None, body=None, state=True):
        """argus-ui's health answers `version` (or the raw `body`); returns the argus-ui row of `deployed`."""
        self.p = load_plugin()
        port, asked = self.health_server(lambda path: (200, {"Content-Type": "application/json"},
                                                       body if body is not None else json.dumps({"ok": True, "version": version}).encode()))
        self.config({"http": "http://127.0.0.1:%d/api/health" % port})
        if state:
            self.state("4.33.0")
        else:
            (self.data / "state" / "you--argus-ui.json").unlink(missing_ok=True)
        return self.p.snapshot(self.now)["deployed"][0], asked

    def test_running_and_drift(self):
        for version, drift in (("4.33.0", "same"), ("4.36.0", "newer"), ("4.30.1", "older"), ("4.33", "same"), ("4.36.0-rc1", "unknown")):
            d, asked = self.runs(version)
            self.assertEqual((d["running"], d["drift"]), ({"version": version}, drift), version)
            self.assertEqual(asked, ["/api/health"], "GET of the configured path, once")
        d, _ = self.runs("4.36.0", state=False)
        self.assertEqual((d["running"], d["drift"], d["deployed"]), ({"version": "4.36.0"}, "no-record", None))
        for body in (b"<html>not json</html>", b'["4.36.0"]', b'{"version": 436}', b'{"version": "<b>4</b>"}', b'{"ok": true}', b""):
            d, _ = self.runs(body=body)
            self.assertEqual((d["running"], d["drift"]), (None, "unknown"), body)
        hermes = self.p.snapshot(self.now)["deployed"][1]
        self.assertEqual((hermes["running"], hermes["drift"]), (None, "unknown"), "no http health: nothing is asked")

    def test_a_slow_or_refused_or_huge_answer_is_unknown(self):
        self.state("4.33.0")
        port, _ = self.health_server(lambda path: 3)
        self.config({"http": "http://127.0.0.1:%d/" % port})
        t0 = time.monotonic()
        d = self.p.snapshot(self.now)["deployed"][0]
        self.assertLess(time.monotonic() - t0, 2.0, "1 s in all, then unknown")
        self.assertEqual((d["running"], d["drift"]), (None, "unknown"))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]
        s.close()                                                   # nothing listens there now: refused
        self.p = load_plugin()
        self.config({"http": "http://127.0.0.1:%d/api/health" % closed})
        self.assertEqual(self.p.snapshot(self.now)["deployed"][0]["drift"], "unknown")
        self.p = load_plugin()
        port, _ = self.health_server(lambda path: (200, {}, b'{"version": "4.36.0", "pad": "' + b"x" * 70000 + b'"}'))
        self.config({"http": "http://127.0.0.1:%d/" % port})
        self.assertIsNone(self.p.snapshot(self.now)["deployed"][0]["running"], "more than 64 KB is not read")

    def test_only_127_0_0_1_is_ever_asked(self):
        port, asked = self.health_server(lambda path: (200, {}, b'{"version": "4.36.0"}'))
        calls = []
        real = socket.create_connection
        for url in ("http://localhost:%d/" % port, "http://127.0.0.2:%d/" % port, "https://127.0.0.1:%d/" % port,
                    "http://127.0.0.1:%d@192.0.2.1/" % port, "http://192.0.2.1:%d/" % port, "http://[::1]:%d/" % port,
                    "http://127.0.0.1/", "http://127.0.0.1:99999/", "http://127.0.0.1:%d/a b" % port, "ftp://127.0.0.1:%d/" % port):
            self.p = load_plugin()
            self.config({"http": url})
            with mock.patch.object(self.p.socket, "create_connection", lambda *a, **kw: calls.append(a) or real(*a, **kw)):
                d = self.p.snapshot(self.now)["deployed"][0]
            self.assertEqual((d["running"], d["drift"]), (None, "unknown"), url)
        self.assertEqual((calls, asked), ([], []), "no connection is opened for an address that is not 127.0.0.1")

    def test_a_redirect_is_not_followed(self):
        port2, asked2 = self.health_server(lambda path: (200, {}, b'{"version": "4.36.0"}'))
        port, asked = self.health_server(lambda path: (302, {"Location": "http://127.0.0.1:%d/api/health" % port2}, b""))
        self.config({"http": "http://127.0.0.1:%d/api/health" % port})
        self.state("4.33.0")
        d = self.p.snapshot(self.now)["deployed"][0]
        self.assertEqual((d["running"], d["drift"], asked, asked2), (None, "unknown", ["/api/health"], []))

    def test_the_answer_is_kept_30_seconds(self):
        clock = [1000.0]
        self.p._tick = lambda: clock[0]
        answer = {"v": "4.36.0"}
        port, asked = self.health_server(lambda path: (200, {}, json.dumps({"version": answer["v"]}).encode()))
        self.config({"http": "http://127.0.0.1:%d/api/health" % port})
        self.state("4.33.0")
        self.assertEqual(self.p.snapshot(self.now)["deployed"][0]["running"], {"version": "4.36.0"})
        answer["v"] = "4.37.0"
        clock[0] += 29
        self.assertEqual(self.p.snapshot(self.now)["deployed"][0]["running"], {"version": "4.36.0"}, "kept")
        self.assertEqual(len(asked), 1)
        clock[0] += 2
        self.assertEqual(self.p.snapshot(self.now)["deployed"][0]["running"], {"version": "4.37.0"}, "asked again after 30 s")
        self.assertEqual(len(asked), 2)

    def test_a_request_older_than_what_runs_is_stale_and_needs_nobody(self):
        port, _ = self.health_server(lambda path: (200, {}, b'{"version": "4.36.0"}'))
        self.config({"http": "http://127.0.0.1:%d/api/health" % port})
        self.state("4.33.0")
        fri = self.now + timedelta(days=2)
        self.rec("20261021-090000-argus-ui-aaaaaaa", version="4.33.0", expires=stamp(fri))
        before = (self.data / "queue" / "20261021-090000-argus-ui-aaaaaaa.json").read_bytes()
        s = self.p.snapshot(self.now)
        self.assertEqual([r["stale"] for r in s["waiting"]], [True])
        day = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[fri.weekday()]
        self.assertEqual(s["line"], "Nothing waiting · 1 old request, expires %s · argus-ui 4.36.0 running" % day)
        self.assertNotIn(" live", s["line"])
        self.assertEqual(self.p.get("status", {}, Ctx())[1], {"line": s["line"], "needs": False, "action": "", "count": 0})
        self.assertEqual(self.p.get("summary", {}, Ctx())[1], {"asks": 0, "deploys": 0, "needs_you": 0, "line": s["line"], "stale": 1})
        self.assertEqual((self.data / "queue" / "20261021-090000-argus-ui-aaaaaaa.json").read_bytes(), before, "a read writes nothing")
        # a newer one arrives: it needs him; the old one still does not count
        self.rec("20261022-090000-argus-ui-aaaaaab", version="4.37.0")
        self.rec("20261022-090001-argus-ui-aaaaaac", version="4.36.0", expires=stamp(self.now + timedelta(hours=1)))
        s = self.p.snapshot(self.now)
        self.assertEqual([(r["version"], r["stale"]) for r in s["waiting"]], [("4.36.0", True), ("4.37.0", False), ("4.33.0", True)])
        self.assertEqual(s["line"], "1 deploy waiting · 2 old requests, expire by %s · argus-ui 4.36.0 running" % day)
        st = self.p.get("status", {}, Ctx())[1]
        self.assertEqual((st["needs"], st["action"], st["count"]), (True, "Review argus-ui 4.37.0", 1))
        self.assertEqual(self.p.get("summary", {}, Ctx())[1]["deploys"], 1)

    def test_without_an_answer_nothing_is_stale_and_the_old_keys_keep_their_meaning(self):
        self.state("4.33.0")
        self.rec("20261021-090000-argus-ui-aaaaaaa", version="4.30.0")
        s = self.p.snapshot(self.now)
        self.assertEqual([r["stale"] for r in s["waiting"]], [False], "unknown is never stale: Zeus's record is not what runs")
        self.assertEqual(s["line"], "1 deploy waiting · argus-ui 4.33.0 deployed")
        d = s["deployed"][0]
        self.assertEqual((d["repo"], d["kind"], d["deployed"]["version"], d["previous"]["version"], d["running"], d["drift"]),
                         ("you/argus-ui", "git", "4.33.0", "4.32.0", None, "unknown"))
        for k in ("at", "line", "asks", "asked", "waiting", "deploying", "deployed", "history", "alert"):
            self.assertIn(k, s)
        self.assertEqual(set(self.p.summary()) - {"stale"}, {"asks", "deploys", "needs_you", "line"})

    def test_a_write_never_asks_what_runs(self):
        self.config({"http": "http://127.0.0.1:1/api/health"})
        with mock.patch.object(self.p, "_ask_version", side_effect=AssertionError("post() asked a health address")):
            for path, body in (("approve", {"id": "20261021-090000-argus-ui-aaaaaaa", "sha": SHA}), ("rollback", {"repo": "you/argus-ui", "sha": PREV}),
                               ("decline", {"id": "20261021-090000-argus-ui-aaaaaaa", "reason": "not-now"}), ("recheck", {"repo": "you/argus-ui"}), ("seen", {})):
                self.assertEqual(self.call(path, body)[0], 200, path)

    def test_manifest(self):
        m = json.loads((R / "plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual((m["id"], m["kind"]), ("throne", "server"))
        self.assertEqual(m["version"], (R / "VERSION").read_text(encoding="utf-8").strip())


if __name__ == "__main__":
    unittest.main()
