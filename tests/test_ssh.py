"""The ssh approval path (docs/design.md §4 "From ssh"; issue #6): the address from SSH_CONNECTION, no user (Janus takes
it from whois), a typed yes, a --user from a shell refused, and `zeus deploy <repo>` = request + approve in one go.
Janus is the fake in tests/fixtures/janus.py with ZEUS_FAKE_JANUS_NODES as the approved devices."""
import io
import json
import os
import subprocess
import sys
import unittest
from datetime import timedelta
from unittest import mock

from zeus import approve, commands, log, queue
from tests.test_approve import ApproveBase, IP, USER
from tests.test_queue import NOW, ROOT

PHONE_IP, OTHER_IP = "203.0.113.7", "203.0.113.99"
OWNER = "owner@" + "example.com"


def yes(answer="yes\n"):
    return lambda rec: approve.ask(rec, read=io.StringIO(answer).readline, write=lambda t: None)


class SshTest(ApproveBase):
    """ApproveBase's temporary HOME, bare remote, fakes and helpers, with approved devices for the fake Janus."""
    def setUp(self):
        super().setUp()
        env = mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS_NODES": json.dumps({PHONE_IP: ["phone", OWNER],
                                                                                IP: ["phone", USER]})})
        env.start()
        self.addCleanup(env.stop)

    def ssh(self, ip):
        who = approve.caller(None, None, env={"SSH_CONNECTION": "%s 52044 192.0.2.10 22" % ip})
        self.assertIsNone(who.error)
        return who

    def approve_ssh(self, rid, ip=PHONE_IP, answer="yes\n", sha=None):
        who = self.ssh(ip)
        self.out = []
        return approve.approve(self.store, self.cfg, rid, sha, who.user, who.ip, now=NOW + timedelta(minutes=3),
                               via=who.via, out=self.out.append, confirm=yes(answer))

    # who is asking
    def test_caller_from_ssh_connection(self):
        who = self.ssh(PHONE_IP)
        self.assertEqual((who.via, who.user, who.ip), ("ssh", None, PHONE_IP))

    def test_a_user_flag_in_an_ssh_session_is_refused(self):
        env = {"SSH_CONNECTION": "%s 52044 192.0.2.10 22" % PHONE_IP}
        for user, ip in ((OWNER, None), (None, PHONE_IP), (OWNER, PHONE_IP)):
            who = approve.caller(user, ip, env=env)
            self.assertIn("refused in an ssh session", who.error)

    def test_the_throne_passes_user_and_ip_outside_ssh(self):
        who = approve.caller(OWNER, PHONE_IP, env={})
        self.assertEqual((who.via, who.user, who.ip), ("elaia", OWNER, PHONE_IP))
        self.assertIn("no identity to check", approve.caller(None, None, env={}).error)
        self.assertIn("no identity to check", approve.caller(OWNER, None, env={}).error)

    # approve over ssh
    def test_allowed_from_an_approved_node(self):
        sha, rec = self.pending()
        self.assertEqual(self.approve_ssh(rec["id"]), 0, self.out)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "approved")
        self.assertEqual(got["approval"]["by"], {"user": OWNER, "ip": PHONE_IP, "via": "ssh", "node": "phone"})
        self.assertEqual(self.janus_requests()[0], {"action": "deploy", "repo": "you/app", "sha": sha, "user": None,
                                                    "ip": PHONE_IP, "when": "2026-10-21T09:03:12-06:00"})
        entry = log.entries(self.store)[0][2]
        self.assertEqual(entry["by"]["via"], "ssh")
        self.assertEqual(entry["by"]["user"], OWNER, "the user in the log is Janus's whois answer, never a flag")
        self.assertTrue(log.check(entry))
        self.assertEqual(len(self.runs()), 1)

    def test_refused_from_another_node(self):
        sha, rec = self.pending()
        self.assertEqual(self.approve_ssh(rec["id"], ip=OTHER_IP), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "pending")
        self.assertEqual(got["janus"]["verdict"], "refused")
        self.assertEqual(log.entries(self.store), [])
        self.assertEqual(self.runs(), [])

    def test_anything_but_yes_cancels_before_janus(self):
        sha, rec = self.pending()
        for answer in ("no\n", "y\n", "YES\n", "\n", ""):
            self.assertEqual(self.approve_ssh(rec["id"], answer=answer), 1, answer)
            self.assertEqual(self.out[-1], "zeus approve: cancelled; nothing was approved.")
        self.assertEqual(self.store.read(rec["id"])["state"], "pending")
        self.assertEqual(self.janus_requests(), [], "Janus is asked only after the yes")
        self.assertEqual(log.entries(self.store), [])

    def test_the_prompt_names_repo_version_sha_and_changes(self):
        sha, rec = self.pending()
        shown = []
        approve.ask(dict(rec, rules_changed=["zeus/repos.json"]), read=lambda: "yes\n", write=shown.append)
        self.assertEqual(shown, ["Deploy you/app 4.9.0 (%s, 2 files, changes the realm's own rules)? type yes: " % sha[:7]])

    def test_a_wrong_sha_over_ssh_is_still_refused(self):
        sha, rec = self.pending()
        self.assertEqual(self.approve_ssh(rec["id"], sha="c" * 40), 1)
        self.assertIn("is not the one the page showed", self.out[0])

    # zeus deploy <repo>
    def test_deploy_requests_and_approves_in_one_go(self):
        sha = self.remote.commit("4.9.0")
        self.out = []
        code = approve.deploy(self.store, self.cfg, "app", self.ssh(PHONE_IP), now=NOW, out=self.out.append, confirm=yes())
        self.assertEqual(code, 0, self.out)
        [rec] = [r for r in self.store.history() + [self.store.read(p.stem) for p in self.store.queue.glob("*.json")]
                 if r and r["sha"] == sha]
        self.assertEqual(rec["state"], "approved")
        self.assertEqual(rec["approval"]["by"]["via"], "ssh")
        self.assertTrue(self.out[0].startswith(rec["id"] + ": pending"), self.out)
        self.assertTrue(self.out[-1].startswith(rec["id"] + ": approved"), self.out)

    def test_deploy_approves_the_request_already_waiting(self):
        sha, rec = self.pending()
        code = approve.deploy(self.store, self.cfg, "you/app", self.ssh(PHONE_IP), now=NOW + timedelta(minutes=1),
                              out=self.out.append, confirm=yes())
        self.assertEqual(code, 0, self.out)
        self.assertEqual(self.store.read(rec["id"])["state"], "approved")

    def test_deploy_an_unknown_repo_fetches_nothing(self):
        calls = []
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args) or (1, "", "fake")):
            code = approve.deploy(self.store, self.cfg, "argus-ui", self.ssh(PHONE_IP), out=self.out.append, confirm=yes())
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(self.out, ["zeus deploy: argus-ui is not one repository in repos.json; nothing was fetched."])

    def test_deploy_cancelled_leaves_the_request_waiting(self):
        sha = self.remote.commit("4.9.0")
        code = approve.deploy(self.store, self.cfg, "app", self.ssh(PHONE_IP), now=NOW, out=self.out.append,
                              confirm=yes("no\n"))
        self.assertEqual(code, 1)
        self.assertEqual([r["sha"] for r in self.store.pending()], [sha])

    # held records
    def test_a_held_record_expires_and_is_superseded(self):
        sha, rec = self.pending()
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "failed"}):
            self.approve_ssh(rec["id"])
        self.assertEqual(self.store.read(rec["id"])["state"], "held")
        queue.expire(self.store, NOW + timedelta(hours=73))
        self.assertEqual(self.store.read(rec["id"])["state"], "expired", "held still expires")
        sha2, rec2 = self.pending("4.9.1")
        with mock.patch.dict(os.environ, {"ZEUS_FAKE_JANUS": "failed"}):
            self.approve_ssh(rec2["id"])
        self.remote.commit("4.9.2")
        commands.request(self.store, self.cfg, "you/app", now=NOW + timedelta(minutes=9))
        self.assertEqual(self.store.read(rec2["id"])["state"], "superseded", "held is superseded by a newer main")


class SshCommandTest(unittest.TestCase):
    """The refusals that happen before any config or fetch, as processes."""
    def run_cli(self, *args, ssh=True):
        env = {k: v for k, v in os.environ.items() if k != "SSH_CONNECTION"}
        if ssh:
            env["SSH_CONNECTION"] = "%s 52044 192.0.2.10 22" % PHONE_IP
        return subprocess.run([sys.executable, "-m", "zeus", *args], cwd=ROOT, capture_output=True, text=True, env=env,
                              input="")

    def test_user_flag_from_a_shell_is_refused(self):
        for args in (("approve", "x", "--user", OWNER), ("approve", "x", "--ip", PHONE_IP), ("deploy", "app", "--user", OWNER)):
            r = self.run_cli(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("--user and --ip are refused in an ssh session", r.stderr)

    def test_no_identity_outside_ssh_is_refused(self):
        r = self.run_cli("approve", "x", ssh=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no identity to check", r.stderr)
        r = self.run_cli("approve", "x", "--user", OWNER, "--ip", PHONE_IP, ssh=False)
        self.assertEqual(r.returncode, 1)
        self.assertIn("--sha is required from the Throne", r.stderr)


if __name__ == "__main__":
    unittest.main()
