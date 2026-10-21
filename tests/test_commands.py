"""zeus request, decline and status (docs/design.md §3, §4; docs/contract.md; issue #4), on a local bare repository
standing in for GitHub, with the same temporary HOME as the queue tests."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from zeus import commands, config, init, log, queue
from tests.test_queue import EXAMPLE, NOW, ROOT, Remote

RE_ID = r"^\d{8}-\d{6}-app-[0-9a-f]{7}(-\d+)?$"


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        self.secret = self.home / ".config" / "zeus" / "secret.env"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                           "ZEUS_DATA": str(self.data), "ZEUS_SECRET": str(self.secret),
                                           "ZEUS_CONFIG": str(self.home / "repos.json"),
                                           "LEAFY_REALM": str(self.home / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        self.remote = Remote(self.tmp.name)
        self.store = queue.Store()
        init.run(lambda s: None)
        self.cfg = config.Config(self.home / "repos.json")
        self.cfg.repos["you/app"] = config.Repo("you/app", {"url": str(self.remote.bare), "path": str(self.home / "src" / "app"),
                                                            "health": {"exec": "zeus check-config"}})
        self.out = []

    def request(self, repo="you/app", now=NOW):
        self.out = []
        return commands.request(self.store, self.cfg, repo, now=now, out=self.out.append)

    def pending_one(self, version="4.9.0"):
        sha = self.remote.commit(version)
        rec = self.request()
        self.assertEqual(rec["state"], "pending", self.out)
        return sha, rec

    # request
    def test_request_makes_a_pending_record_from_the_commit_itself(self):
        sha = self.remote.commit("4.9.0", files={"CHANGELOG.md": "# Changelog\n\n## 4.9.0\n- Learn cards\n- Fixes\n\n## 4.8.0\n- old\n"})
        rec = self.request()
        self.assertEqual(rec["state"], "pending")
        self.assertRegex(rec["id"], RE_ID)
        self.assertEqual((rec["repo"], rec["sha"], rec["version"], rec["checks"]), ("you/app", sha, "4.9.0", "none"))
        self.assertEqual(rec["subject"], "version 4.9.0")
        self.assertEqual(rec["changelog"], ["- Learn cards", "- Fixes"])
        self.assertIsNone(rec["previous"])
        self.assertEqual(rec["seen"], queue.stamp(NOW))
        self.assertTrue(rec["verified"]["on_main"])
        self.assertEqual(self.store.pending()[0]["id"], rec["id"])
        self.assertTrue(self.out[0].startswith(rec["id"] + ": pending (you/app 4.9.0"), self.out)

    def test_request_takes_previous_from_the_deployed_state(self):
        first = self.remote.commit("4.8.0")
        (self.data / "state").mkdir(parents=True, exist_ok=True)
        (self.data / "state" / "you--app.json").write_text(json.dumps({"deployed": {"sha": first, "version": "4.8.0"}}))
        second = self.remote.commit("4.9.0", files={"docs/x.md": "x"})
        rec = self.request()
        self.assertEqual(rec["previous"], first)
        self.assertEqual(rec["files"], 3)

    def test_request_on_an_unknown_repo_is_refused_without_a_fetch(self):
        calls = []
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args) or (1, "", "fake")):
            rec = self.request("someone/else")
        self.assertIsNone(rec)
        self.assertEqual(calls, [])
        self.assertEqual(self.out, ["zeus request: someone/else is not in repos.json; nothing was fetched."])
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.history(), [])

    def test_request_with_a_broken_config_fetches_nothing(self):
        calls = []
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args) or (1, "", "fake")):
            rec = commands.request(self.store, None, "you/app", out=self.out.append)
        self.assertIsNone(rec)
        self.assertEqual(calls, [])

    def test_request_twice_for_the_same_commit_is_a_duplicate(self):
        sha, first = self.pending_one()
        rec = self.request()
        self.assertEqual(rec["state"], "refused")
        self.assertIn("already waiting as " + first["id"], rec["reason"])

    def test_request_after_a_new_commit_supersedes(self):
        sha, first = self.pending_one()
        self.remote.commit("4.9.1")
        rec = self.request(now=NOW + timedelta(minutes=1))
        self.assertEqual(rec["version"], "4.9.1")
        self.assertEqual(self.store.read(first["id"])["state"], "superseded")

    def test_request_when_the_fetch_fails(self):
        self.cfg.repos["you/app"].url = str(Path(self.tmp.name) / "nowhere.git")
        self.assertIsNone(self.request())
        self.assertTrue(self.out[0].startswith("zeus request: could not fetch main of "), self.out)

    def test_changelog_lines_are_the_first_section_only_and_capped(self):
        text = "# Changelog\n\n## 1.0.0\n" + "".join("- line %d\n" % i for i in range(15)) + "\n## 0.9.0\n- old\n"
        lines = commands.changelog_lines(text)
        self.assertEqual(len(lines), 10)
        self.assertEqual(lines[0], "- line 0")
        self.assertEqual(commands.changelog_lines(None), [])
        self.assertEqual(commands.changelog_lines("no sections\n- stray\n"), [])

    # decline
    def test_decline_a_pending_request_writes_a_signed_line(self):
        sha, rec = self.pending_one()
        self.out = []
        code = commands.decline(self.store, rec["id"], "not-now", user="dax@" + "example.com", ip="203.0.113.5",
                                now=NOW + timedelta(minutes=2), out=self.out.append)
        self.assertEqual(code, 0, self.out)
        gone = self.store.read(rec["id"])
        self.assertEqual(gone["state"], "declined")
        self.assertEqual(gone["reason"], "not-now")
        self.assertEqual(gone["approval"]["decision"], "declined")
        self.assertEqual(gone["approval"]["by"], {"user": "dax@" + "example.com", "via": "elaia", "ip": "203.0.113.5"})
        self.assertTrue((self.store.done / (rec["id"] + ".json")).is_file())
        self.assertEqual(self.store.pending(), [])
        rows = log.entries(self.store)
        self.assertEqual(len(rows), 1)
        entry = rows[0][2]
        self.assertEqual((entry["action"], entry["id"], entry["repo"], entry["sha"], entry["version"], entry["reason"],
                          entry["janus"]), ("decline", rec["id"], "you/app", sha, "4.9.0", "not-now", "not asked"))
        self.assertEqual(entry["t"], "2026-10-21T09:02:12-06:00")
        self.assertTrue(log.check(entry))
        self.assertEqual(self.out[0], rec["id"] + ": declined (you/app 4.9.0): not-now")

    def test_decline_from_a_shell_records_the_shell_user(self):
        sha, rec = self.pending_one()
        commands.decline(self.store, rec["id"], "typo", out=self.out.append)
        by = log.entries(self.store)[0][2]["by"]
        self.assertEqual(by["via"], "ssh")
        self.assertTrue(by["user"])
        self.assertNotIn("ip", by)

    def test_decline_never_calls_janus_or_git(self):
        sha, rec = self.pending_one()
        with mock.patch.object(queue, "git", lambda args, **kw: self.fail("git was called")), \
             mock.patch.object(subprocess, "run", lambda *a, **kw: self.fail("a subprocess was started")):
            self.assertEqual(commands.decline(self.store, rec["id"], "no", out=self.out.append), 0)

    def test_decline_a_record_that_is_not_pending_is_refused(self):
        sha, rec = self.pending_one()
        commands.decline(self.store, rec["id"], "first", out=self.out.append)
        self.out = []
        self.assertEqual(commands.decline(self.store, rec["id"], "again", out=self.out.append), 1)
        self.assertEqual(self.out, ["zeus decline: %s is declined, not pending; only a waiting request can be declined."
                                    % rec["id"]])
        self.assertEqual(len(log.entries(self.store)), 1, "no second line")

    def test_decline_an_expired_request_is_refused(self):
        sha, rec = self.pending_one()
        self.out = []
        self.assertEqual(commands.decline(self.store, rec["id"], "late", now=NOW + timedelta(hours=80), out=self.out.append), 1)
        self.assertIn("is expired, not pending", self.out[0])

    def test_decline_unknown_id_and_bad_reason(self):
        self.assertEqual(commands.decline(self.store, "nope", "x", out=self.out.append), 1)
        self.assertEqual(self.out, ["zeus decline: no request nope."])
        self.out = []
        for bad in ("", "Not Now", "two words", "a" * 33, "sh;rm"):
            self.assertEqual(commands.decline(self.store, "nope", bad, out=self.out.append), 2, bad)
        self.assertIn("one word of lowercase letters, digits and dashes", self.out[0])

    def test_decline_without_a_secret_changes_nothing(self):
        sha, rec = self.pending_one()
        self.secret.unlink()
        self.out = []
        self.assertEqual(commands.decline(self.store, rec["id"], "no", out=self.out.append), 1)
        self.assertIn("run `zeus init`", self.out[0])
        self.assertEqual(self.store.read(rec["id"])["state"], "pending")

    # status
    def test_status_document_shape(self):
        sha, rec = self.pending_one()
        doc = commands.status(self.store, self.cfg, now=NOW + timedelta(minutes=1))
        self.assertEqual(sorted(doc), ["at", "deployed", "deploying", "history", "line", "queue", "zeus"])
        self.assertEqual(doc["deployed"], {"you/app": None})
        self.assertEqual([r["id"] for r in doc["queue"]], [rec["id"]])
        self.assertEqual(doc["deploying"], [])
        self.assertEqual(doc["history"], [])
        self.assertEqual(doc["line"], "1 deploy waiting")
        self.assertEqual(doc["at"], "2026-10-21T09:01:12-06:00")
        json.dumps(doc)

    def test_status_applies_expiry_and_lists_history_newest_first_capped_at_ten(self):
        for i in range(12):
            self.remote.commit("4.%d.0" % i)
            self.request(now=NOW + timedelta(seconds=i))
        doc = commands.status(self.store, self.cfg, now=NOW + timedelta(hours=100))
        self.assertEqual(doc["queue"], [])
        self.assertEqual(doc["line"], "nothing waiting")
        self.assertEqual(len(doc["history"]), 10)
        self.assertEqual(doc["history"][0]["state"], "expired")
        self.assertEqual(doc["history"][0]["version"], "4.11.0")
        self.assertEqual(doc["history"][1]["state"], "superseded")

    def test_status_shows_what_is_live(self):
        (self.data / "state").mkdir(parents=True, exist_ok=True)
        (self.data / "state" / "you--app.json").write_text(json.dumps({"deployed": {"sha": "a" * 40, "version": "4.8.0",
                                                                                     "at": "2026-10-20T10:00:00-06:00"}}))
        doc = commands.status(self.store, self.cfg, now=NOW)
        self.assertEqual(doc["deployed"]["you/app"]["version"], "4.8.0")
        self.assertEqual(doc["line"], "nothing waiting · app 4.8.0 live")
        self.assertIn("  live      you/app: 4.8.0 (aaaaaaa) since 2026-10-20T10:00:00-06:00", commands.text(doc))

    def test_status_text(self):
        sha, rec = self.pending_one()
        text = commands.text(commands.status(self.store, self.cfg, now=NOW))
        self.assertTrue(text.startswith("1 deploy waiting\n  waiting   " + rec["id"]), text)
        self.assertIn("  live      you/app: nothing deployed by Zeus yet", text)


class CommandLineTest(unittest.TestCase):
    """The three commands as processes with the example repos.json: no network is reached."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        self.repos = self.home / "repos.json"
        self.home.mkdir()
        self.repos.write_text(EXAMPLE.read_text())
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), ZEUS_DATA=str(self.data),
                        ZEUS_SECRET=str(self.home / "secret.env"), ZEUS_CONFIG=str(self.repos),
                        LEAFY_REALM=str(self.home / "none.json"))

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "zeus", *args], cwd=ROOT, capture_output=True, text=True, env=self.env)

    def test_request_unknown_repo(self):
        r = self.run_cli("request", "someone/else")
        self.assertEqual(r.returncode, 1)
        self.assertIn("someone/else is not in repos.json; nothing was fetched.", r.stdout)
        self.assertFalse((self.data / "mirrors").exists())

    def test_decline_unknown_id(self):
        r = self.run_cli("decline", "nope", "--reason", "x")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no request nope", r.stdout)
        r = self.run_cli("decline", "nope")
        self.assertEqual(r.returncode, 2)

    def test_status_json_and_text(self):
        r = self.run_cli("status", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        doc = json.loads(r.stdout)
        self.assertEqual(sorted(doc["deployed"]), ["you/argus-ui", "you/hermes", "you/janus", "you/realm", "you/zeus"])
        self.assertEqual(doc["queue"], [])
        self.assertEqual(doc["line"], "nothing waiting")
        r = self.run_cli("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("nothing waiting\n"), r.stdout)

    def test_status_with_a_broken_config_still_prints_and_exits_1(self):
        doc = json.loads(EXAMPLE.read_text())
        doc["repos"]["you/argus-ui"]["branch"] = "dev"
        self.repos.write_text(json.dumps(doc))
        r = self.run_cli("status", "--json")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(json.loads(r.stdout)["deployed"], {})
        self.assertIn("run `zeus check-config`", r.stderr)


if __name__ == "__main__":
    unittest.main()
