"""zeus queue (docs/design.md §3, issue #3): request messages become records, Zeus checks main for himself against a
local bare repository standing in for GitHub, a newer commit supersedes, a stale one expires, a repository outside the
allowlist never triggers a fetch, and requests/ is empty after every run."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from zeus import config, queue

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "repos.example.json"
GIT_ID = ["-c", "user.name=Test", "-c", "user.email=test@" + "example.com"]
NOW = datetime(2026, 10, 21, 9, 0, 12, tzinfo=timezone(timedelta(hours=-6)))


def git(*args, cwd):
    return subprocess.run(["git", *GIT_ID, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Remote:
    """A bare repository as the "GitHub" remote, with a working clone to make commits on main (and on a branch)."""
    def __init__(self, tmp):
        self.bare = Path(tmp) / "remote" / "app.git"
        self.work = Path(tmp) / "work"
        self.bare.parent.mkdir()
        git("init", "-q", "--bare", str(self.bare), cwd=tmp)
        git("clone", "-q", str(self.bare), str(self.work), cwd=tmp)
        git("checkout", "-q", "-b", "main", cwd=self.work)
        self.shas = []

    def commit(self, version, files=None, branch=None):
        """One commit on main (or on a branch); returns its sha. Each commit bumps VERSION and touches CHANGELOG."""
        if branch:
            git("checkout", "-q", "-B", branch, cwd=self.work)
        (self.work / "VERSION").write_text(version + "\n")
        with open(self.work / "CHANGELOG.md", "a") as f:
            f.write("## %s\n" % version)
        for name, text in (files or {}).items():
            p = self.work / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git("add", "-A", cwd=self.work)
        git("commit", "-q", "-m", "version " + version, cwd=self.work)
        sha = git("rev-parse", "HEAD", cwd=self.work)
        git("push", "-q", str(self.bare), "HEAD:refs/heads/" + (branch or "main"), cwd=self.work)
        if branch:
            git("checkout", "-q", "main", cwd=self.work)
        else:
            self.shas.append(sha)
        return sha


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                           "ZEUS_DATA": str(self.data), "ZEUS_CONFIG": str(self.home / "repos.json"),
                                           "LEAFY_REALM": str(self.home / "none.json")})
        env.start()
        self.addCleanup(env.stop)
        self.remote = Remote(self.tmp.name)
        self.store = queue.Store()
        self.store.make_folders()
        self.cfg = config.Config(self.home / "repos.json")
        self.clone = self.home / "src" / "app"
        self.cfg.repos["you/app"] = config.Repo("you/app", {"url": str(self.remote.bare), "path": str(self.clone),
                                                            "health": {"exec": "zeus check-config"},
                                                            "rules": ["zeus/repos.json"]})

    def message(self, sha, previous=None, version="4.9.0", repo="you/app", seen=None, **extra):
        msg = {"type": "deploy-request", "repo": repo, "sha": sha, "previous": previous, "subject": "Mentor: Learn phase",
               "version": version, "checks": "success", "changelog": ["- Learn cards"],
               "seen": queue.stamp(seen or NOW)}
        msg.update(extra)
        path = self.store.requests / ("%s-%s.json" % (repo.split("/")[-1], sha[:7]))
        path.write_text(json.dumps(msg))
        return path

    def run_queue(self, now=NOW, cfg="default"):
        out = []
        recs = queue.process(self.store, self.cfg if cfg == "default" else cfg, now=now, out=out.append)
        self.assertEqual(list(self.store.requests.iterdir()), [], "requests/ must be empty after every run")
        return recs, out

    # the happy path
    def test_a_commit_on_main_becomes_a_pending_record(self):
        sha = self.remote.commit("4.9.0")
        self.message(sha)
        recs, out = self.run_queue()
        rec = recs[0]
        self.assertEqual(rec["state"], "pending")
        self.assertEqual(rec["id"], "20261021-090012-app-" + sha[:7])
        self.assertEqual((rec["repo"], rec["sha"], rec["version"], rec["checks"]), ("you/app", sha, "4.9.0", "success"))
        self.assertTrue(rec["verified"]["on_main"])
        self.assertEqual(rec["expires"], "2026-10-24T09:00:12-06:00")
        self.assertEqual(rec["files"], 2)                      # VERSION and CHANGELOG.md in the root commit
        self.assertEqual(rec["rules_changed"], [])
        self.assertIsNone(rec["approval"])
        self.assertTrue((self.store.queue / (rec["id"] + ".json")).is_file())
        self.assertTrue((self.store.mirrors / "you" / "app.git" / "HEAD").is_file(), "a bare mirror before the first deploy")
        self.assertFalse(self.clone.exists(), "the clone is made by the first deploy, not by the queue")
        self.assertEqual(out[0], rec["id"] + ": pending (you/app 4.9.0, 2 files); expires 2026-10-24T09:00:12-06:00")
        self.assertEqual(queue.listing(self.store, NOW), [rec])

    def test_files_and_rules_changed_come_from_the_diff(self):
        first = self.remote.commit("4.9.0")
        second = self.remote.commit("4.9.1", files={"zeus/repos.json": "{}", "docs/x.md": "x"})
        self.message(second, previous=first, version="4.9.1")
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["files"], 4)
        self.assertEqual(recs[0]["rules_changed"], ["zeus/repos.json"])
        self.assertIn("(changes the realm's own rules)", out[0])

    def test_an_existing_clone_is_fetched_instead_of_a_mirror(self):
        sha = self.remote.commit("4.9.0")
        git("clone", "-q", str(self.remote.bare), str(self.clone), cwd=self.tmp.name)
        later = self.remote.commit("4.9.1")
        self.message(later, previous=sha, version="4.9.1")
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["state"], "pending")
        self.assertFalse((self.store.mirrors / "you").exists())
        self.assertEqual(git("rev-parse", "origin/main", cwd=self.clone), later)

    # refusals
    def test_a_commit_on_a_branch_is_refused(self):
        self.remote.commit("4.9.0")
        sha = self.remote.commit("4.9.1", branch="feature/x")
        self.message(sha, version="4.9.1")
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["state"], "refused")
        self.assertEqual(recs[0]["reason"], "commit %s is not on main of you/app" % sha[:7])
        self.assertFalse(recs[0]["verified"]["on_main"])
        self.assertTrue((self.store.done / (recs[0]["id"] + ".json")).is_file())
        self.assertEqual(self.store.pending(), [])

    def test_a_sha_that_does_not_exist_is_refused(self):
        self.remote.commit("4.9.0")
        self.message("f" * 40)
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["state"], "refused")
        self.assertIn("is not on main", recs[0]["reason"])

    def test_a_repo_not_in_the_allowlist_never_triggers_a_fetch(self):
        self.message("a" * 40, repo="someone/else")
        calls = []
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args) or (1, "", "fake git")):
            recs, out = self.run_queue()
        self.assertEqual(calls, [], "no git call at all for a repository outside repos.json")
        self.assertEqual(recs[0]["state"], "refused")
        self.assertEqual(recs[0]["reason"], "someone/else is not in repos.json; nothing was fetched")
        self.assertEqual(recs[0]["repo"], "someone/else")

    def test_a_broken_repos_json_refuses_without_fetching(self):
        self.message("a" * 40)
        calls = []
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args) or (1, "", "fake git")):
            recs, out = self.run_queue(cfg=None)
        self.assertEqual(calls, [])
        self.assertEqual(recs[0]["state"], "refused")
        self.assertIn("repos.json has errors", recs[0]["reason"])

    def test_only_the_allowlisted_url_is_fetched(self):
        sha = self.remote.commit("4.9.0")
        self.message(sha, url="https://evil.example/x.git")
        calls = []
        real = queue.git
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args) or real(args, **kw)):
            recs, out = self.run_queue()
        fetches = [c for c in calls if c[0] == "fetch"]
        self.assertEqual(len(fetches), 1)
        self.assertIn(str(self.remote.bare), fetches[0])
        self.assertEqual(fetches[0][-1], "+refs/heads/main:refs/remotes/origin/main")

    def test_a_fetch_that_fails_is_refused_with_gits_words(self):
        self.cfg.repos["you/app"].url = str(Path(self.tmp.name) / "nowhere.git")
        self.message("a" * 40)
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["state"], "refused")
        self.assertTrue(recs[0]["reason"].startswith("could not fetch main of "), recs[0]["reason"])

    def test_bad_json_and_wrong_messages_are_refused_but_leave_requests(self):
        (self.store.requests / "app-abc1234.json").write_text("{ not json")
        (self.store.requests / "note.json").write_text(json.dumps({"type": "note", "text": "hi"}))
        (self.store.requests / "list.json").write_text("[1, 2]")
        self.message("abc", version="x")                      # a short sha
        self.message("b" * 40, changelog="not a list", checks=5)
        recs, out = self.run_queue()
        self.assertEqual([r["state"] for r in recs], ["refused"] * 5)
        reasons = "\n".join(r["reason"] for r in recs)
        self.assertIn("app-abc1234.json: not valid JSON", reasons)
        self.assertIn("note.json: not a deploy-request message (type is 'note')", reasons)
        self.assertIn("list.json: not a JSON object", reasons)
        self.assertIn("sha must be 40 hex characters", reasons)
        self.assertIn("checks must be text; changelog must be a list of lines", reasons)
        self.assertEqual(len(list(self.store.done.glob("*.json"))), 5)

    def test_a_duplicate_of_a_waiting_request_is_refused(self):
        sha = self.remote.commit("4.9.0")
        self.message(sha)
        first = self.run_queue()[0][0]
        self.message(sha)
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["state"], "refused")
        self.assertEqual(recs[0]["reason"], "already waiting as %s; nothing was fetched" % first["id"])
        self.assertEqual([r["id"] for r in self.store.pending()], [first["id"]])

    # superseding and expiry
    def test_a_second_commit_supersedes_the_first(self):
        first = self.remote.commit("4.9.0")
        self.message(first)
        old = self.run_queue()[0][0]
        second = self.remote.commit("4.9.1")
        self.message(second, previous=first, version="4.9.1")
        new = self.run_queue(now=NOW + timedelta(minutes=5))[0][0]
        self.assertEqual(new["state"], "pending")
        gone = self.store.read(old["id"])
        self.assertEqual(gone["state"], "superseded")
        self.assertEqual(gone["reason"], "replaced by 4.9.1")
        self.assertEqual(gone["superseded_by"]["id"], new["id"])
        self.assertTrue((self.store.done / (old["id"] + ".json")).is_file())
        self.assertFalse((self.store.queue / (old["id"] + ".json")).exists())
        self.assertEqual([r["id"] for r in self.store.pending()], [new["id"]])

    def test_a_refused_newer_commit_does_not_supersede(self):
        first = self.remote.commit("4.9.0")
        self.message(first)
        old = self.run_queue()[0][0]
        branch = self.remote.commit("4.9.1", branch="feature/x")
        self.message(branch, version="4.9.1")
        self.run_queue(now=NOW + timedelta(minutes=5))
        self.assertEqual(self.store.read(old["id"])["state"], "pending")

    def test_another_repository_is_not_superseded(self):
        other = config.Repo("you/other", {"url": str(self.remote.bare), "path": str(self.home / "src" / "other"),
                                          "health": {"exec": "zeus check-config"}})
        self.cfg.repos["you/other"] = other
        first = self.remote.commit("1.0.0")
        self.message(first, repo="you/other", version="1.0.0")
        second = self.remote.commit("4.9.0")
        self.message(second, version="4.9.0")
        self.run_queue()
        self.assertEqual(sorted(r["repo"] for r in self.store.pending()), ["you/app", "you/other"])

    def test_an_expired_record_is_listed_as_expired(self):
        sha = self.remote.commit("4.9.0")
        self.message(sha)
        rec = self.run_queue()[0][0]
        self.assertEqual(queue.listing(self.store, NOW + timedelta(hours=71)), [rec])
        self.assertEqual(queue.listing(self.store, NOW + timedelta(hours=72)), [])
        gone = self.store.read(rec["id"])
        self.assertEqual(gone["state"], "expired")
        self.assertEqual(gone["reason"], "expired 2026-10-24T09:00:12-06:00")
        self.assertEqual(self.store.history()[0]["id"], rec["id"])

    def test_expiry_uses_the_config_hours_and_the_message_seen_time(self):
        self.cfg.expire_hours = 1
        sha = self.remote.commit("4.9.0")
        self.message(sha, seen=NOW - timedelta(hours=2))
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["expires"], "2026-10-21T08:00:12-06:00")
        self.assertEqual(self.store.read(recs[0]["id"])["state"], "expired", "already stale when it arrived")
        self.assertIn("expired", out[1])

    def test_a_message_without_seen_takes_now(self):
        sha = self.remote.commit("4.9.0")
        self.message(sha, seen=None)
        path = self.store.requests / ("app-%s.json" % sha[:7])
        msg = json.loads(path.read_text())
        del msg["seen"]
        path.write_text(json.dumps(msg))
        recs, out = self.run_queue()
        self.assertEqual(recs[0]["seen"], "2026-10-21T09:00:12-06:00")

    def test_two_requests_in_one_second_get_distinct_ids(self):
        first = self.remote.commit("4.9.0")
        self.message(first)
        second = self.remote.commit("4.9.1", branch="feature/x")
        self.message(second, version="4.9.1")
        recs, out = self.run_queue()
        self.assertEqual(len({r["id"] for r in recs}), 2)

    def test_nothing_waiting_writes_nothing(self):
        recs, out = self.run_queue()
        self.assertEqual(recs, [])
        self.assertEqual(out, [])


class QueueCommandTest(unittest.TestCase):
    """zeus queue as a process, with a real repos.json (the example): no network is reached in these cases."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        self.repos = self.home / "repos.json"
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), ZEUS_DATA=str(self.data),
                        ZEUS_CONFIG=str(self.repos), LEAFY_REALM=str(self.home / "none.json"))
        self.requests = self.data / "requests"
        self.requests.mkdir(parents=True)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "zeus", "queue", *args], cwd=ROOT, capture_output=True, text=True,
                              env=self.env)

    def test_nothing_waiting(self):
        self.repos.write_text(EXAMPLE.read_text())
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "zeus queue: nothing waiting")

    def test_a_request_outside_the_allowlist_is_refused_and_the_folder_empties(self):
        self.repos.write_text(EXAMPLE.read_text())
        (self.requests / "x-abc.json").write_text(json.dumps({"type": "deploy-request", "repo": "someone/x", "sha": "a" * 40}))
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("-x-aaaaaaa: refused (someone/x aaaaaaa): someone/x is not in repos.json; nothing was fetched", r.stdout)
        self.assertEqual(list(self.requests.iterdir()), [])
        self.assertEqual(len(list((self.data / "queue" / ".done").glob("*.json"))), 1)

    def test_a_broken_repos_json_refuses_everything_and_exits_1(self):
        doc = json.loads(EXAMPLE.read_text())
        doc["repos"]["you/argus-ui"]["branch"] = "dev"
        self.repos.write_text(json.dumps(doc))
        (self.requests / "argus-ui-abc.json").write_text(json.dumps({"type": "deploy-request", "repo": "you/argus-ui",
                                                                      "sha": "a" * 40}))
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("every waiting request is refused until it is fixed", r.stderr)
        self.assertIn("repos.json has errors", r.stdout)
        self.assertEqual(list(self.requests.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
