"""Failed deploys and rolling back (docs/design.md §6; issue #9): a build failure holds with nothing changed, an install
failure puts the previous approved commit back, a double failure alerts with both versions, and `zeus rollback` by hand
goes one step back on the stack (refused when there is nothing to go back to). Built on the deploy tests' fixtures:
the fixture script writes a marker as its "install", and FAKE_DEPLOY_ONLY makes only one version fail."""
import io
import json
import os
from datetime import timedelta
from unittest import mock

from zeus import approve, deploy, log, rollback
from tests.test_approve import IP, USER
from tests.test_deploy import DeployBase
from tests.test_queue import NOW, git

PHONE_IP = "203.0.113.7"
OWNER = "owner@" + "example.com"


class RollbackTest(DeployBase):
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS_NODES": json.dumps({PHONE_IP: ["phone", OWNER],
                                                                                IP: ["phone", USER]})})
        env.start()
        self.addCleanup(env.stop)

    def live(self, *versions):
        """Deploy each version in turn; returns their shas."""
        shas = []
        for v in versions:
            sha, rec = self.approved(v)
            self.assertEqual(self.deploy(rec["id"]), 0, self.out)
            shas.append(sha)
        return shas

    def state(self):
        return json.loads((self.data / "state" / "you--app.json").read_text())

    def ssh(self):
        return approve.caller(None, None, env={"SSH_CONNECTION": "%s 50000 192.0.2.10 22" % PHONE_IP})

    def roll(self, answer="yes\n", sha=None, who=None, now=None):
        self.out = []
        confirm = (lambda live, prev: rollback.ask(live, prev, read=io.StringIO(answer).readline, write=lambda t: None))
        return rollback.rollback(self.store, self.cfg, "app", who or self.ssh(), sha=sha, now=now or NOW + timedelta(hours=1),
                                 out=self.out.append, confirm=confirm if (who or self.ssh()).via == "ssh" else None)

    def rollback_record(self):
        recs = [r for r in self.store.history() if r.get("action") == "rollback"]
        recs += [json.loads(p.read_text()) for p in self.store.queue.glob("*.json")]
        return [r for r in recs if r.get("action") == "rollback"][0]

    # hold vs rollback
    def test_a_build_failure_holds_and_the_marker_is_untouched(self):
        first, = self.live("4.9.0")
        sha, rec = self.approved("4.9.1")
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "fail-build"}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "failed")
        self.assertFalse(got["deploy"]["install_stage"])
        self.assertEqual(self.marker.read_text().split()[0], first, "the install never ran: 4.9.0 is still what runs")
        self.assertEqual(self.state()["deployed"]["sha"], first)
        self.assertNotIn("putting back", self.runlog(rec["id"]))
        self.assertIn("failed before the install began, so nothing was changed; holding", self.runlog(rec["id"]))
        self.assertIn("app 4.9.1 failed before the install; nothing was changed", json.loads(self.alert.read_text())["message"])

    def test_an_install_failure_puts_the_previous_back(self):
        first, = self.live("4.9.0")
        sha, rec = self.approved("4.9.1")
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "fail-install", "FAKE_DEPLOY_ONLY": "4.9.1"}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "rolled-back")
        self.assertEqual(got["reason"], "scripts/deploy.sh exited 4 after the install had begun; 4.9.0 was put back and is healthy.")
        self.assertEqual(got["deploy"]["restored"], {"sha": first, "version": "4.9.0"})
        self.assertEqual(self.marker.read_text().split(), [first, rec["id"], sha, "4.9.0"], "the marker shows the previous sha")
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), first)
        st = self.state()
        self.assertEqual(st["deployed"]["sha"], first, "the failed commit never became deployed")
        text = self.runlog(rec["id"])
        self.assertIn("failed after the install had begun: scripts/deploy.sh exited 4", text)
        self.assertIn("putting back 4.9.0 (%s), the commit that was live before this run, because the install of 4.9.1 "
                      "had begun" % first[:7], text)
        self.assertIn("rolled back: you/app 4.9.0 runs again", text)
        self.assertEqual([e["outcome"] for _, _, e in log.entries(self.store, kind="deploys")], ["deployed", "rolled-back"])
        self.assertIn("app 4.9.1 failed and 4.9.0 was put back", json.loads(self.alert.read_text())["message"])

    def test_a_timeout_and_a_health_failure_also_roll_back(self):
        first, = self.live("4.9.0")
        sha, rec = self.approved("4.9.1")
        self.cfg.deploy_timeout_s = 1
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "hang", "FAKE_DEPLOY_ONLY": "4.9.1"}):
            self.deploy(rec["id"])
        self.assertEqual(self.store.read(rec["id"])["state"], "rolled-back")
        self.cfg.deploy_timeout_s = 20
        sha, rec = self.approved("4.9.2")
        real = deploy.health_once

        def health(shape, value, want):          # 4.9.2 never comes up; 4.9.0 does
            return "not up" if want == "4.9.2" else real(shape, value, want)
        with mock.patch.object(deploy, "health_once", health):
            self.deploy(rec["id"])
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "rolled-back")
        self.assertTrue(got["reason"].startswith("the health check did not pass in 2 s: not up"), got["reason"])
        self.assertEqual(self.marker.read_text().split()[0], first)

    def test_a_double_failure_alerts_with_both_versions(self):
        first, = self.live("4.9.0")
        sha, rec = self.approved("4.9.1")
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "fail-install"}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "failed")
        self.assertEqual(got["reason"], "scripts/deploy.sh exited 4 after the install had begun; 4.9.1 failed and 4.9.0 "
                                        "could not be restored.")
        alert = json.loads(self.alert.read_text())
        self.assertEqual(alert["message"], "app 4.9.1 failed and 4.9.0 could not be restored")
        self.assertEqual(alert["link"], "throne")
        self.assertIn("putting back 4.9.0 failed too", self.runlog(rec["id"]))
        self.assertIn("Nothing more is tried", self.runlog(rec["id"]))
        self.assertEqual(self.state()["deployed"]["sha"], first)

    def test_never_retried(self):
        first, = self.live("4.9.0")
        sha, rec = self.approved("4.9.1")
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "fail-install"}):
            self.deploy(rec["id"])
        self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertIn("is failed, not approved; nothing was run", self.out[0])
        self.assertEqual(len(self.runs()), 2, "systemd-run was started only by the two approvals")

    # zeus rollback by hand
    def test_rollback_by_hand_goes_one_step_back(self):
        first, second = self.live("4.9.0", "4.9.1")
        self.assertEqual(self.roll(), 0, self.out)
        rec = self.rollback_record()
        self.assertEqual((rec["action"], rec["state"], rec["sha"], rec["version"]), ("rollback", "approved", first, "4.9.0"))
        self.assertEqual(rec["from"], {"sha": second, "version": "4.9.1"})
        self.assertEqual(self.janus_requests()[-1]["action"], "rollback")
        self.assertIsNone(self.janus_requests()[-1]["user"])
        self.assertEqual(self.janus_requests()[-1]["via"], "ssh", "without via Janus takes it for the page and refuses")
        entry = [e for _, _, e in log.entries(self.store) if e["action"] == "rollback"][0]
        self.assertTrue(log.check(entry))
        self.assertEqual(entry["by"]["user"], OWNER)
        self.assertEqual(self.runs()[-1][1], "--unit=zeus-deploy-" + rec["id"])
        self.assertEqual(self.deploy(rec["id"]), 0, self.out)
        self.assertEqual(self.store.read(rec["id"])["state"], "deployed")
        st = self.state()
        self.assertEqual(st["deployed"]["sha"], first)
        self.assertIsNone(st["previous"])
        self.assertEqual(self.marker.read_text().split()[0], first)
        self.assertEqual(git("rev-parse", "HEAD", cwd=self.clone), first)
        self.assertIn("rolled back to you/app 4.9.0", self.out[-1])

    def test_rolling_back_twice_goes_one_step_further(self):
        a, b, c = self.live("4.9.0", "4.9.1", "4.9.2")
        self.assertEqual(self.roll(), 0, self.out)
        self.assertEqual(self.deploy(self.rollback_record()["id"]), 0, self.out)
        self.assertEqual((self.state()["deployed"]["sha"], self.state()["previous"]["sha"]), (b, a))
        self.assertEqual(self.roll(now=NOW + timedelta(hours=2)), 0, self.out)
        rid = [r for r in self.store.pending(("approved",)) if r.get("action") == "rollback"][0]["id"]
        self.assertEqual(self.deploy(rid), 0, self.out)
        self.assertEqual(self.state()["deployed"]["sha"], a)
        self.assertEqual(self.marker.read_text().split()[0], a)

    def test_rollback_is_refused_when_there_is_no_previous(self):
        self.live("4.9.0")
        self.assertEqual(self.roll(), 1)
        self.assertEqual(self.out, ["zeus rollback: you/app has no previous approved commit to go back to; nothing was done."])
        self.assertEqual(self.janus_requests()[-1]["action"], "deploy", "Janus was not asked")

    def test_rollback_cancelled_wrong_target_and_janus_refused(self):
        first, second = self.live("4.9.0", "4.9.1")
        n = len(self.janus_requests())
        self.assertEqual(self.roll(answer="no\n"), 1)
        self.assertEqual(self.out, ["zeus rollback: cancelled; nothing was done."])
        self.assertEqual(len(self.janus_requests()), n)
        throne = approve.caller(OWNER, PHONE_IP, env={})
        self.assertEqual(self.roll(who=throne, sha=second), 1)
        self.assertIn("is not the one the page showed", self.out[0])
        self.assertEqual(self.roll(who=throne), 1)
        self.assertIn("--sha is required from the Throne", self.out[0])
        other = approve.caller(None, None, env={"SSH_CONNECTION": "203.0.113.99 1 2 3"})
        self.assertEqual(self.roll(who=other), 1)
        self.assertIn("Janus refused", self.out[0])
        self.assertEqual(self.state()["deployed"]["sha"], second)

    def test_a_stale_rollback_is_refused_by_deploy_run(self):
        first, second = self.live("4.9.0", "4.9.1")
        self.assertEqual(self.roll(), 0, self.out)
        rid = self.rollback_record()["id"]
        self.live("4.9.2")                                    # something else lands before the rollback runs
        self.assertEqual(self.deploy(rid), 1)
        self.assertIn("is no longer %s" % first[:7], self.store.read(rid)["reason"])
        self.assertEqual(self.state()["deployed"]["version"], "4.9.2")

    def test_a_failed_hand_rollback_restores_what_ran(self):
        first, second = self.live("4.9.0", "4.9.1")
        self.roll()
        rid = self.rollback_record()["id"]
        with mock.patch.dict(os.environ, {"FAKE_DEPLOY": "fail-install", "FAKE_DEPLOY_ONLY": "4.9.0"}):
            self.assertEqual(self.deploy(rid), 1)
        self.assertEqual(self.store.read(rid)["state"], "rolled-back")
        self.assertEqual(self.marker.read_text().split()[0], second, "4.9.1, which ran before the rollback, runs again")


if __name__ == "__main__":
    import unittest
    unittest.main()
