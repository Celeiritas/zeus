"""zeus approve (docs/design.md §4, docs/contract.md; issue #5): freshness, the sha the page showed, the on-main
re-check, Janus's three answers, the signed line, and the detached deploy start. Janus and systemd-run are the fakes
in tests/fixtures/, named through ZEUS_JANUS and ZEUS_SYSTEMD_RUN as JSON lists so the test runs on any OS."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from zeus import approve, commands, config, init, log, queue
from tests.test_queue import EXAMPLE, NOW, ROOT, Remote, git

FIX = ROOT / "tests" / "fixtures"
USER = "dax@" + "example.com"
IP = "203.0.113.5"


class ApproveBase(unittest.TestCase):
    """A temporary HOME, a bare remote, the fakes, and the helpers; no tests of its own (test_ssh builds on it)."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        self.secret = self.home / ".config" / "zeus" / "secret.env"
        self.alert = self.home / "alerts" / "zeus.json"
        self.janus_log = self.home / "janus.log"
        self.run_log = self.home / "systemd-run.log"
        env = mock.patch.dict(os.environ, {
            "HOME": str(self.home), "USERPROFILE": str(self.home), "ZEUS_DATA": str(self.data),
            "ZEUS_SECRET": str(self.secret), "ZEUS_CONFIG": str(self.home / "repos.json"),
            "LEAFY_REALM": str(self.home / "none.json"), "ZEUS_ALERT": str(self.alert),
            "ZEUS_JANUS": json.dumps([sys.executable, str(FIX / "janus.py")]),
            "ZEUS_SYSTEMD_RUN": json.dumps([sys.executable, str(FIX / "systemd_run.py")]),
            "ZEUS_FAKE_JANUS": "allowed", "ZEUS_FAKE_JANUS_LOG": str(self.janus_log),
            "ZEUS_FAKE_SYSTEMD_RUN": "ok", "ZEUS_FAKE_SYSTEMD_RUN_LOG": str(self.run_log), "ZEUS_BIN": "/usr/local/bin/zeus"})
        env.start()
        self.addCleanup(env.stop)
        self.remote = Remote(self.tmp.name)
        self.store = queue.Store()
        init.run(lambda s: None)
        self.cfg = config.Config(self.home / "repos.json")
        self.cfg.repos["you/app"] = config.Repo("you/app", {"url": str(self.remote.bare), "path": str(self.home / "src" / "app"),
                                                            "health": {"exec": "zeus check-config"}})
        self.out = []

    def pending(self, version="4.9.0"):
        sha = self.remote.commit(version)
        rec = commands.request(self.store, self.cfg, "you/app", now=NOW)
        self.assertEqual(rec["state"], "pending")
        return sha, rec

    def approve(self, rid, sha, now=None, user=USER, ip=IP):
        self.out = []
        return approve.approve(self.store, self.cfg, rid, sha, user, ip, now=now or NOW + timedelta(minutes=3),
                               out=self.out.append)

    def janus_requests(self):
        return [json.loads(l) for l in self.janus_log.read_text().splitlines()] if self.janus_log.exists() else []

    def runs(self):
        return [json.loads(l) for l in self.run_log.read_text().splitlines()] if self.run_log.exists() else []


class ApproveTest(ApproveBase):
    # allowed
    def test_allowed_approves_logs_and_starts_the_deploy(self):
        sha, rec = self.pending()
        self.assertEqual(self.approve(rec["id"], sha), 0, self.out)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "approved")
        self.assertEqual(got["approval"]["decision"], "approved")
        self.assertEqual(got["approval"]["by"], {"user": USER, "ip": IP, "via": "elaia", "node": "phone"})
        self.assertEqual(got["approval"]["janus"], "allowed")
        self.assertEqual(got["janus"]["verdict"], "allowed")
        self.assertTrue(got["verified"]["on_main"])
        self.assertEqual(got["deploy"]["started"], True)
        self.assertEqual(got["deploy"]["unit"], "zeus-deploy-" + rec["id"])
        self.assertTrue((self.store.queue / (rec["id"] + ".json")).is_file(), "approved is not terminal: it stays in queue/")
        # Janus was asked exactly what the design says, nothing more
        self.assertEqual(self.janus_requests(), [{"action": "deploy", "repo": "you/app", "sha": sha, "user": USER, "ip": IP,
                                                  "when": "2026-10-21T09:03:12-06:00"}])
        # the signed line
        rows = log.entries(self.store)
        self.assertEqual(len(rows), 1)
        entry = rows[0][2]
        self.assertEqual((entry["action"], entry["id"], entry["repo"], entry["sha"], entry["version"], entry["janus"]),
                         ("deploy", rec["id"], "you/app", sha, "4.9.0", "allowed"))
        self.assertEqual(entry["by"]["node"], "phone")
        self.assertTrue(log.check(entry))
        self.assertEqual(got["approval"]["sig"], entry["sig"])
        # the detached start
        self.assertEqual(self.runs(), [["--user", "--unit=zeus-deploy-" + rec["id"], "--collect", "-p", "CPUQuota=50%",
                                        "-p", "MemoryMax=1G", "-p", "IOWeight=50", "/usr/local/bin/zeus", "deploy-run", rec["id"]]])
        self.assertFalse(self.alert.exists())
        self.assertEqual(self.out[0], "%s: approved (you/app 4.9.0) by %s; deploying as zeus-deploy-%s" % (rec["id"], USER, rec["id"]))

    def test_the_sha_is_rechecked_on_origin_main_before_janus(self):
        sha, rec = self.pending()
        calls = []
        real = queue.git
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args[0]) or real(args, **kw)):
            self.assertEqual(self.approve(rec["id"], sha), 0, self.out)
        self.assertIn("fetch", calls)
        self.assertIn("merge-base", calls)

    def test_a_second_approve_of_the_same_record_is_refused(self):
        sha, rec = self.pending()
        self.assertEqual(self.approve(rec["id"], sha), 0, self.out)
        self.assertEqual(self.approve(rec["id"], sha), 1)
        self.assertEqual(self.out, ["zeus approve: %s is approved, not pending; an approval is for one commit, once." % rec["id"]])
        self.assertEqual(len(log.entries(self.store)), 1)
        self.assertEqual(len(self.runs()), 1)

    # refusals before Janus
    def test_wrong_sha_is_refused_without_asking_janus(self):
        sha, rec = self.pending()
        self.assertEqual(self.approve(rec["id"], "b" * 40), 1)
        self.assertIn("is not the one the page showed", self.out[0])
        self.assertEqual(self.store.read(rec["id"])["state"], "pending")
        self.assertEqual(self.janus_requests(), [])
        self.assertEqual(log.entries(self.store), [])
        self.assertEqual(self.approve(rec["id"], None), 1)
        self.assertEqual(self.approve(rec["id"], sha[:7]), 1)

    def test_expired_is_refused(self):
        sha, rec = self.pending()
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(hours=72)), 1)
        self.assertIn("is expired, not pending", self.out[0])
        self.assertEqual(self.store.read(rec["id"])["state"], "expired")
        self.assertEqual(self.janus_requests(), [])

    def test_superseded_is_refused(self):
        sha, rec = self.pending()
        self.remote.commit("4.9.1")
        commands.request(self.store, self.cfg, "you/app", now=NOW + timedelta(minutes=1))
        self.assertEqual(self.approve(rec["id"], sha), 1)
        self.assertIn("is superseded, not pending", self.out[0])
        self.assertEqual(self.janus_requests(), [])

    def test_declined_and_unknown_are_refused(self):
        sha, rec = self.pending()
        commands.decline(self.store, rec["id"], "no")
        self.assertEqual(self.approve(rec["id"], sha), 1)
        self.assertIn("is declined, not pending", self.out[0])
        self.assertEqual(self.approve("nope", sha), 1)
        self.assertEqual(self.out, ["zeus approve: no request nope."])

    def test_a_commit_no_longer_on_main_is_refused_at_approval_time(self):
        sha, rec = self.pending()
        # main is force-moved to an unrelated commit between the request and the yes
        git("checkout", "-q", "--orphan", "fresh", cwd=self.remote.work)
        (self.remote.work / "VERSION").write_text("9.0.0\n")
        git("add", "-A", cwd=self.remote.work)
        git("commit", "-q", "-m", "rewritten", cwd=self.remote.work)
        git("push", "-q", "-f", str(self.remote.bare), "HEAD:refs/heads/main", cwd=self.remote.work)
        self.assertEqual(self.approve(rec["id"], sha), 1)
        self.assertIn("is no longer on main of you/app", self.out[0])
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "refused")
        self.assertFalse(got["verified"]["on_main"])
        self.assertEqual(self.janus_requests(), [])
        self.assertEqual(log.entries(self.store), [])

    def test_a_repo_missing_from_config_and_a_failed_fetch(self):
        sha, rec = self.pending()
        self.assertEqual(approve.approve(self.store, None, rec["id"], sha, USER, IP, out=self.out.append), 1)
        self.assertIn("not in repos.json; nothing was fetched", self.out[0])
        self.cfg.repos["you/app"].url = str(Path(self.tmp.name) / "gone.git")
        self.assertEqual(self.approve(rec["id"], sha), 1)
        self.assertIn("could not fetch main of", self.out[0])
        self.assertEqual(self.store.read(rec["id"])["state"], "pending")

    # Janus's answers
    def test_janus_refused_keeps_the_record_pending_with_the_reasons(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "refused"}):
            self.assertEqual(self.approve(rec["id"], sha), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "pending")
        self.assertEqual(got["janus"]["verdict"], "refused")
        self.assertEqual(got["janus"]["reasons"], ["%s is not in approvers.zeus.users" % USER,
                                                   "node for %s is not in approvers.zeus.nodes" % IP])
        self.assertEqual(got["janus"]["user"], USER)
        self.assertIsNone(got["approval"])
        self.assertEqual(log.entries(self.store), [])
        self.assertEqual(self.runs(), [])
        self.assertFalse(self.alert.exists())
        self.assertEqual(self.out[0], "zeus approve: Janus refused: %s is not in approvers.zeus.users; node for %s is not "
                                      "in approvers.zeus.nodes" % (USER, IP))

    def test_janus_refused_with_plain_text_reasons(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "plain"}):
            self.assertEqual(self.approve(rec["id"], sha), 1)
        self.assertEqual(self.store.read(rec["id"])["janus"]["reasons"], ["user is not an approver", "node is not approved"])

    def test_janus_failed_holds_the_record_and_writes_the_alert(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "failed"}):
            self.assertEqual(self.approve(rec["id"], sha), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "held")
        self.assertEqual(got["reason"], "held: tailscale whois failed")
        self.assertTrue((self.store.queue / (rec["id"] + ".json")).is_file(), "held stays in queue/")
        alert = json.loads(self.alert.read_text())
        self.assertEqual(alert["link"], "throne")
        self.assertEqual(alert["message"], "app 4.9.0: Janus could not verify the approval (tailscale whois failed); the request is held")
        self.assertEqual(log.entries(self.store), [])
        self.assertEqual(self.runs(), [])
        self.assertEqual(queue.listing(self.store, NOW), [], "a held record is not shown as waiting")

    def test_a_held_record_can_be_approved_once_janus_answers(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "failed"}):
            self.approve(rec["id"], sha)
        self.assertEqual(self.approve(rec["id"], sha), 0, self.out)
        self.assertEqual(self.store.read(rec["id"])["state"], "approved")
        self.assertFalse(self.alert.exists(), "the alert clears on the yes")

    def test_janus_garbage_and_hang_count_as_failed(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "garbage"}):
            self.assertEqual(self.approve(rec["id"], sha), 0, "exit 0 is allowed even when the text is odd")
        sha2, rec2 = self.pending("4.9.1")
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "hang"}), mock.patch.object(approve, "JANUS_TIMEOUT", 1):
            self.assertEqual(self.approve(rec2["id"], sha2), 1)
        self.assertEqual(self.store.read(rec2["id"])["state"], "held")
        self.assertIn("no answer in 1 s", self.store.read(rec2["id"])["reason"])

    def test_janus_missing_counts_as_failed(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_JANUS": json.dumps([str(self.home / "no-such-janus")])}):
            self.assertEqual(self.approve(rec["id"], sha), 1)
        self.assertEqual(self.store.read(rec["id"])["state"], "held")
        self.assertIn("Janus could not be run", self.store.read(rec["id"])["reason"])

    # the start
    def test_systemd_run_failure_keeps_the_approval_and_alerts(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_SYSTEMD_RUN": "fail"}):
            self.assertEqual(self.approve(rec["id"], sha), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "approved")
        self.assertEqual(got["deploy"]["started"], False)
        self.assertIn("already exists", got["deploy"]["error"])
        self.assertEqual(len(log.entries(self.store)), 1, "the yes was given and is logged")
        self.assertIn("could not be started", json.loads(self.alert.read_text())["message"])
        self.assertIn("the deploy did not start", self.out[0])

    def test_command_from_the_environment(self):
        with mock.patch.dict(os.environ, {"ZEUS_JANUS": ""}):
            self.assertEqual(approve.command("ZEUS_JANUS", "janus"), ["janus"])
        with mock.patch.dict(os.environ, {"ZEUS_JANUS": "[1, 2]"}):
            self.assertEqual(approve.command("ZEUS_JANUS", "janus"), ["janus"])
        with mock.patch.dict(os.environ, {"ZEUS_JANUS": '["python", "x.py"]'}):
            self.assertEqual(approve.command("ZEUS_JANUS", "janus"), ["python", "x.py"])
        with mock.patch.dict(os.environ, {"ZEUS_JANUS": "janus-dev"}):
            self.assertEqual(approve.command("ZEUS_JANUS", "janus"), ["janus-dev"])


class ApproveCommandTest(unittest.TestCase):
    """zeus approve as a process: the flags are required; an unknown record is refused with the example config."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        (self.home / "repos.json").write_text(EXAMPLE.read_text())
        base = {k: v for k, v in os.environ.items() if k != "SSH_CONNECTION"}   # the server runs the tests over ssh
        self.env = dict(base, HOME=str(self.home), USERPROFILE=str(self.home),
                        ZEUS_DATA=str(self.home / "data"), ZEUS_SECRET=str(self.home / "secret.env"),
                        ZEUS_CONFIG=str(self.home / "repos.json"), LEAFY_REALM=str(self.home / "none.json"))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "zeus", "approve", *args], cwd=ROOT, capture_output=True, text=True,
                              env=self.env)

    def test_the_throne_must_give_user_ip_and_sha(self):
        r = self.run_cli("some-id")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no identity to check", r.stderr)
        r = self.run_cli("some-id", "--sha", "a" * 40, "--user", "me")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no identity to check", r.stderr)
        r = self.run_cli("some-id", "--user", "me", "--ip", IP)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--sha is required from the Throne", r.stderr)

    def test_unknown_record(self):
        r = self.run_cli("nope", "--sha", "a" * 40, "--user", "me", "--ip", IP)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertEqual(r.stdout.strip(), "zeus approve: no request nope.")


if __name__ == "__main__":
    unittest.main()
