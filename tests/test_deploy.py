"""zeus deploy-run (docs/design.md §5; issue #8): a local bare repository as the remote carrying the fixture
scripts/deploy.sh (which writes a marker as its "install"), the fake Janus and systemd-run for the approval, and two
health checks: the fixed exec list (pointed at a stand-in through ZEUS_HEALTH_ZEUS) and an http server on 127.0.0.1."""
import http.server
import json
import os
import sys
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from zeus import config, deploy, log, queue
from tests.test_approve import ApproveBase, FIX
from tests.test_queue import NOW, git

SCRIPT = (FIX / "deploy.sh").read_text(encoding="utf-8")
OK = json.dumps([sys.executable, "-c", "import sys; sys.exit(0)"])
BAD = json.dumps([sys.executable, "-c", "import sys; sys.exit(1)"])


class Health(http.server.BaseHTTPRequestHandler):
    answer = {"ok": True, "version": "4.9.0"}
    status = 200

    def do_GET(self):
        body = json.dumps(Health.answer).encode()
        self.send_response(Health.status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class DeployBase(ApproveBase):
    """ApproveBase plus the marker, the health stand-in, short timeouts, and helpers to approve and run deploys."""
    def setUp(self):
        super().setUp()
        self.marker = Path(self.tmp.name) / "marker.txt"
        self.clone = self.home / "src" / "app"
        env = mock.patch.dict(os.environ, {"DEPLOY_MARKER": self.marker.as_posix(), "FAKE_DEPLOY": "",
                                           "ZEUS_HEALTH_ZEUS": OK})
        env.start()
        self.addCleanup(env.stop)
        self.cfg.deploy_timeout_s, self.cfg.health_wait_s = 20, 2
        patch = mock.patch.object(deploy, "HEALTH_POLL_S", 0.1)
        patch.start()
        self.addCleanup(patch.stop)

    def approved(self, version="4.9.0", files=None):
        sha = self.remote.commit(version, files=dict({"scripts/deploy.sh": SCRIPT}, **(files or {})))
        from zeus import commands
        rec = commands.request(self.store, self.cfg, "you/app", now=NOW)
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 0, self.out)
        return sha, self.store.read(rec["id"])

    def deploy(self, rid):
        self.out = []
        return deploy.run(self.store, self.cfg, rid, out=self.out.append)

    def runlog(self, rid):
        return (self.data / "log" / ("deploy-%s.log" % rid)).read_text(encoding="utf-8")


class DeployTest(DeployBase):
    # the full run
    def test_a_full_run_ends_deployed_with_the_right_state(self):
        sha, rec = self.approved()
        self.assertEqual(self.deploy(rec["id"]), 0, self.out)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "deployed")
        self.assertTrue((self.store.done / (rec["id"] + ".json")).is_file())
        self.assertEqual(got["deploy"]["outcome"], "deployed")
        self.assertTrue(got["deploy"]["install_stage"])
        self.assertEqual(self.marker.read_text().split(), [sha, rec["id"], "4.9.0"])
        st = json.loads((self.data / "state" / "you--app.json").read_text())
        self.assertEqual(st["deployed"]["sha"], sha)
        self.assertEqual(st["deployed"]["version"], "4.9.0")
        self.assertEqual(st["deployed"]["id"], rec["id"])
        self.assertEqual(st["deployed"]["approval"]["via"], "elaia")
        self.assertIsNone(st["previous"])
        lines = [e for _, _, e in log.entries(self.store, kind="deploys")]
        self.assertEqual([(e["id"], e["outcome"]) for e in lines], [(rec["id"], "deployed")])
        self.assertTrue(log.check(lines[0]))
        text = self.runlog(rec["id"])
        self.assertIn("build " + sha, text)
        self.assertIn("ZEUS_STAGE=install", text)
        self.assertIn("zeus: ", text)
        self.assertIn("health: ok", text)
        self.assertFalse(self.alert.exists())

    def test_the_detached_checkout_is_at_the_sha_and_hooks_never_run(self):
        sha, rec = self.approved()
        self.deploy(rec["id"])
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), sha)
        self.assertEqual(git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.clone), "HEAD", "detached")

    def test_the_second_deploy_moves_deployed_to_previous(self):
        first, rec1 = self.approved("4.9.0")
        self.assertEqual(self.deploy(rec1["id"]), 0, self.out)
        second, rec2 = self.approved("4.9.1")
        self.assertEqual(rec2["previous"], first)
        self.assertEqual(self.deploy(rec2["id"]), 0, self.out)
        st = json.loads((self.data / "state" / "you--app.json").read_text())
        self.assertEqual((st["deployed"]["sha"], st["previous"]["sha"]), (second, first))
        self.assertEqual([h["sha"] for h in st["history"]], [first])
        self.assertEqual(self.marker.read_text().split(), [second, rec2["id"], first, "4.9.1"])

    def test_history_keeps_ten(self):
        st = {"repo": "you/app", "deployed": None, "previous": None, "history": []}
        (self.data / "state").mkdir(parents=True, exist_ok=True)
        for i in range(13):
            deploy.record_deployed(self.store, {"repo": "you/app", "sha": "%02d" % i * 20, "version": str(i), "id": str(i)}, NOW)
        st = json.loads((self.data / "state" / "you--app.json").read_text())
        self.assertEqual(st["deployed"]["version"], "12")
        self.assertEqual(st["previous"]["version"], "11")
        self.assertEqual([h["version"] for h in st["history"]], [str(i) for i in range(11, 1, -1)])

    def test_http_health_with_the_version_match(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.cfg.repos["you/app"].health = {"http": "http://127.0.0.1:%d/api/health" % server.server_port}
        Health.answer = {"ok": True, "version": "4.9.0"}
        sha, rec = self.approved("4.9.0")
        self.assertEqual(self.deploy(rec["id"]), 0, self.out)
        Health.answer = {"ok": True, "version": "4.8.0"}
        sha, rec = self.approved("4.9.1")
        self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertIn("says version 4.8.0, the commit is 4.9.1", self.store.read(rec["id"])["reason"])

    # refusals before anything runs
    def test_a_sha_not_on_main_is_refused_before_anything_runs(self):
        sha, rec = self.approved()
        git("checkout", "-q", "--orphan", "fresh", cwd=self.remote.work)
        git("commit", "-q", "-m", "rewritten", cwd=self.remote.work)
        git("push", "-q", "-f", str(self.remote.bare), "HEAD:refs/heads/main", cwd=self.remote.work)
        self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "failed")
        self.assertIn("is not on main of you/app; nothing was run.", got["reason"])
        self.assertFalse(got["deploy"]["install_stage"])
        self.assertFalse(self.marker.exists())

    def test_a_free_exec_in_health_is_refused(self):
        sha, rec = self.approved()
        self.cfg.repos["you/app"].health = {"exec": "bash -c 'touch /tmp/pwned'"}
        calls = []
        real = queue.git
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args[0]) or real(args, **kw)):
            self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertIn("Zeus never runs a command named in config, so nothing was run.", self.store.read(rec["id"])["reason"])
        self.assertEqual(calls, [], "not even a fetch")
        self.assertFalse(self.marker.exists())

    def test_only_an_approved_and_signed_record_runs(self):
        sha = self.remote.commit("4.9.0", files={"scripts/deploy.sh": SCRIPT})
        from zeus import commands
        rec = commands.request(self.store, self.cfg, "you/app", now=NOW)
        self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertEqual(self.out, ["zeus deploy-run: %s is pending, not approved; nothing was run." % rec["id"]])
        forged = dict(rec, state="approved", approval={"decision": "approved", "sig": "0" * 64})
        self.store.write(forged)
        self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertEqual(self.out, ["zeus deploy-run: %s has no signed approval in the log; nothing was run." % rec["id"]])
        self.assertFalse(self.marker.exists())
        self.assertFalse(self.clone.exists())
        self.assertEqual(self.deploy("nope"), 1)

    def test_submodules_and_a_missing_script_are_refused(self):
        sha, rec = self.approved(files={".gitmodules": "[submodule \"x\"]\n"})
        self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertIn("uses submodules", self.store.read(rec["id"])["reason"])
        git("rm", "-q", "scripts/deploy.sh", ".gitmodules", cwd=self.remote.work)
        git("commit", "-q", "-m", "no script", cwd=self.remote.work)
        git("push", "-q", str(self.remote.bare), "HEAD:refs/heads/main", cwd=self.remote.work)
        from zeus import commands
        rec2 = commands.request(self.store, self.cfg, "you/app", now=NOW + timedelta(minutes=5))
        self.approve(rec2["id"], rec2["sha"], now=NOW + timedelta(minutes=6))
        self.assertEqual(self.deploy(rec2["id"]), 1)
        self.assertIn("has no scripts/deploy.sh", self.store.read(rec2["id"])["reason"])
        self.assertFalse(self.marker.exists())

    def test_a_folder_that_is_not_a_clone_is_refused(self):
        sha, rec = self.approved()
        self.clone.mkdir(parents=True)
        (self.clone / "someone-elses.txt").write_text("x")
        self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertIn("is not empty and is not a git clone", self.store.read(rec["id"])["reason"])

    # failures (hold vs rollback is #9; here they end failed with the stage)
    def test_a_build_failure_ends_failed_before_the_install(self):
        sha, rec = self.approved()
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "fail-build"}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["reason"], "scripts/deploy.sh exited 3 before the install began, so nothing was changed.")
        self.assertFalse(got["deploy"]["install_stage"])
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.data / "state" / "you--app.json").exists())
        self.assertIn("failed", json.loads(self.alert.read_text())["message"])
        self.assertEqual([e["outcome"] for _, _, e in log.entries(self.store, kind="deploys")], ["failed"])

    def test_an_install_failure_records_the_stage(self):
        sha, rec = self.approved()
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "fail-install"}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["reason"], "scripts/deploy.sh exited 4 after the install had begun; 4.9.0 failed and there "
                                        "is no earlier approved commit to put back.")
        self.assertTrue(got["deploy"]["install_stage"])

    def test_a_timeout_stops_the_script(self):
        sha, rec = self.approved()
        self.cfg.deploy_timeout_s = 1
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "hang"}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertEqual(self.store.read(rec["id"])["reason"],
                         "scripts/deploy.sh did not finish in 1 s and was stopped, after the install had begun; "
                         "4.9.0 failed and there is no earlier approved commit to put back.")

    def test_health_that_never_passes(self):
        sha, rec = self.approved()
        with mock.patch.dict(os.environ, {"ZEUS_HEALTH_ZEUS": BAD}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertTrue(got["reason"].startswith("the health check did not pass in 2 s: zeus check-config exited 1"),
                        got["reason"])
        self.assertTrue(self.marker.exists(), "the script ran; the service did not come up")
        self.assertFalse((self.data / "state" / "you--app.json").exists())

    def test_the_command_line(self):
        sha, rec = self.approved()
        args = type("A", (), {"id": rec["id"], "file": None})
        with mock.patch.object(config, "check", lambda path=None: self.cfg):
            self.assertEqual(deploy.main(args), 0)
        self.assertEqual(self.store.read(rec["id"])["state"], "deployed")


if __name__ == "__main__":
    unittest.main()
