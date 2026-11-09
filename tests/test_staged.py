"""Staged packs (docs/design.md §15): `zeus stage-request`, the checks at the request, at the hold and before the
install, the fixed install script (a fake one in tests/fixtures/install-pack.sh, and the real example in
examples/install-pack.sh.example), rollback to the set-aside copy, and check-config for kind staged-pack. A local bare
repository stands in for GitHub; packs are built here with tarfile, the way `tar -czf x.tgz -C dist .` builds them."""
import hashlib
import io
import json
import os
import shutil
import stat
import tarfile
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from zeus import approve, cli, commands, config, deploy, log, queue, rollback, staged
from tests.test_approve import FIX, IP, USER
from tests.test_config import Home
from tests.test_deploy import DeployBase, BAD
from tests.test_queue import NOW, ROOT

EXAMPLE_INSTALL = ROOT / "examples" / "install-pack.sh.example"
GLB = b"glTF" + bytes(range(256)) * 40


class StagedBase(DeployBase):
    """DeployBase plus a staged-pack repository you/game on the same bare remote, its staging folder, and the fake
    install script (or the real example, with use_example())."""
    def setUp(self):
        super().setUp()
        self.staging = self.home / ".config" / "argus-ui" / ".staged-game"
        self.staging.mkdir(parents=True)
        self.install = self.home / ".config" / "zeus" / "install" / "game.sh"
        self.install.parent.mkdir(parents=True)
        shutil.copyfile(FIX / "install-pack.sh", self.install)
        self.imarker = Path(self.tmp.name) / "install.txt"
        env = mock.patch.dict(os.environ, {"INSTALL_MARKER": self.imarker.as_posix(), "FAKE_INSTALL": "", "FAKE_ROLLBACK": ""})
        env.start()
        self.addCleanup(env.stop)
        self.cfg.repos["you/game"] = config.Repo("you/game", {
            "kind": "staged-pack", "url": str(self.remote.bare), "staging": str(self.staging),
            "install": str(self.install), "health": {"exec": "zeus check-config"}})
        self.live_dir = self.home / ".config" / "argus-ui" / "plugins" / "game"

    def use_example(self):
        shutil.copyfile(EXAMPLE_INSTALL, self.install)

    def pack(self, version, sha, files=None, version_file=True, checksum=None, extra=None):
        """Build <staging>/<version>-<sha>.tgz and its .sha256; returns the pack's path."""
        dist = Path(self.tmp.name) / ("dist-%s-%s" % (version, sha[:7]))
        shutil.rmtree(dist, ignore_errors=True)
        (dist / "models").mkdir(parents=True)
        tree = {"index.html": "<html>%s</html>" % version, "models/hero.1a2b3c4d.glb": GLB,
                "assets.json": json.dumps({"version": version, "files": {"models/hero.glb": "models/hero.1a2b3c4d.glb"}}, indent=1)}
        if version_file:
            tree["VERSION"] = version + "\n"
        tree.update(files or {})
        for name, body in tree.items():
            p = dist / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(body if isinstance(body, bytes) else body.encode())
        path = self.staging / ("%s-%s.tgz" % (version, sha))
        with tarfile.open(path, "w:gz") as tf:
            tf.add(dist, arcname=".")
            for info, body in (extra or []):
                tf.addfile(info, io.BytesIO(body) if body is not None else None)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.staging / (path.name + ".sha256")).write_text("%s  %s\n" % (checksum or digest, path.name))
        past = time.time_ns() - 5 * 10 ** 9
        os.utime(path, ns=(past, past))
        return path

    def commit(self, version):
        return self.remote.commit(version)

    def ask(self, version, sha, tests="12 passed", now=None):
        self.out = []
        return staged.request(self.store, self.cfg, "you/game", version, sha, tests, now=now or NOW, out=self.out.append)

    def staged_ok(self, version="1.0.0", tests="12 passed", now=None):
        sha = self.commit(version)
        self.pack(version, sha)
        code, rec = self.ask(version, sha, tests, now=now)
        self.assertEqual((code, rec["state"]), (0, "pending"), (self.out, rec.get("reason")))
        return sha, rec

    def approved_pack(self, version="1.0.0", now=None):
        sha, rec = self.staged_ok(version, now=now)
        self.assertEqual(self.approve(rec["id"], sha, now=(now or NOW) + timedelta(minutes=1)), 0, self.out)
        return sha, self.store.read(rec["id"])

    def installs(self):
        return self.imarker.read_text().splitlines() if self.imarker.exists() else []

    def state(self):
        return json.loads((self.data / "state" / "you--game.json").read_text())


class StageRequestTest(StagedBase):
    def test_a_request_records_the_pack_and_the_commit(self):
        sha, rec = self.staged_ok("1.0.0")
        self.assertEqual((rec["kind"], rec["action"], rec["repo"], rec["sha"], rec["version"]),
                         ("staged-pack", "deploy", "you/game", sha, "1.0.0"))
        self.assertEqual(rec["tests"], "12 passed")
        self.assertEqual(rec["changelog"], [])          # the fixture CHANGELOG has headings only
        self.assertEqual(rec["subject"], "version 1.0.0")
        self.assertTrue(rec["verified"]["on_main"])
        pack = self.staging / ("1.0.0-%s.tgz" % sha)
        st = os.lstat(pack)
        self.assertEqual(rec["pack"]["name"], pack.name)
        self.assertEqual((rec["pack"]["size"], rec["pack"]["mtime_ns"]), (st.st_size, st.st_mtime_ns))
        self.assertEqual(rec["pack"]["sha256"], hashlib.sha256(pack.read_bytes()).hexdigest())
        self.assertEqual(rec["pack"]["files"], 4)
        self.assertLessEqual(st.st_mtime_ns, rec["pack"]["checked_ns"])
        self.assertTrue((self.store.queue / (rec["id"] + ".json")).is_file())
        self.assertIn("pack", self.out[0])

    def test_a_bad_checksum_is_refused(self):
        sha = self.commit("1.0.0")
        self.pack("1.0.0", sha, checksum="0" * 64)
        code, rec = self.ask("1.0.0", sha)
        self.assertEqual((code, rec["state"]), (1, "refused"))
        self.assertIn("is not the one that was staged", rec["reason"])
        self.assertTrue((self.store.done / (rec["id"] + ".json")).is_file(), "a refusal is a record in History")

    def test_a_checksum_file_naming_another_pack_is_refused(self):
        sha = self.commit("1.0.0")
        path = self.pack("1.0.0", sha)
        (self.staging / (path.name + ".sha256")).write_text("%s  other.tgz\n" % hashlib.sha256(path.read_bytes()).hexdigest())
        code, rec = self.ask("1.0.0", sha)
        self.assertIn("names other.tgz", rec["reason"])

    def test_a_commit_not_on_main_is_refused(self):
        self.commit("0.9.0")
        sha = self.remote.commit("1.0.0", branch="feature")
        self.pack("1.0.0", sha)
        code, rec = self.ask("1.0.0", sha)
        self.assertEqual((code, rec["state"]), (1, "refused"))
        self.assertIn("is not on main of you/game", rec["reason"])

    def test_a_missing_version_file_is_refused(self):
        sha = self.commit("1.0.0")
        self.pack("1.0.0", sha, version_file=False)
        code, rec = self.ask("1.0.0", sha)
        self.assertEqual((code, rec["state"]), (1, "refused"))
        self.assertIn("has no VERSION file", rec["reason"])

    def test_a_version_file_that_differs_is_refused(self):
        sha = self.commit("1.0.0")
        self.pack("1.0.0", sha, files={"VERSION": "0.9.0\n"})
        code, rec = self.ask("1.0.0", sha)
        self.assertIn("VERSION says '0.9.0'", rec["reason"])

    def test_a_commit_whose_version_differs_is_refused(self):
        sha = self.commit("1.0.1")
        self.pack("1.0.0", sha)
        code, rec = self.ask("1.0.0", sha)
        self.assertIn("VERSION says '1.0.1', the request says 1.0.0", rec["reason"])

    def test_a_pack_that_is_not_staged_is_refused(self):
        sha = self.commit("1.0.0")
        code, rec = self.ask("1.0.0", sha)
        self.assertEqual(code, 1)
        self.assertIn("is not staged", rec["reason"])

    def test_names_outside_the_pack_links_and_devices_are_refused(self):
        sha = self.commit("1.0.0")
        cases = []
        evil = tarfile.TarInfo("../evil.txt")
        evil.size = 3
        cases.append(([(evil, b"bad")], "a name outside its own folder"))
        absolute = tarfile.TarInfo("/etc/evil")
        absolute.size = 3
        cases.append(([(absolute, b"bad")], "a name outside its own folder"))
        link = tarfile.TarInfo("./models/link.glb")
        link.type, link.linkname = tarfile.SYMTYPE, "../../.ssh/id_ed25519"
        cases.append(([(link, None)], "a link, device or pipe"))
        hard = tarfile.TarInfo("./hard")
        hard.type, hard.linkname = tarfile.LNKTYPE, "./VERSION"
        cases.append(([(hard, None)], "a link, device or pipe"))
        for extra, words in cases:
            for p in self.staging.iterdir():
                p.unlink()
            self.pack("1.0.0", sha, extra=extra)
            code, rec = self.ask("1.0.0", sha)
            self.assertEqual((code, rec["state"]), (1, "refused"))
            self.assertIn(words, rec["reason"])

    def test_a_pack_that_is_not_a_gzip_tar_is_refused(self):
        sha = self.commit("1.0.0")
        path = self.staging / ("1.0.0-%s.tgz" % sha)
        path.write_bytes(b"not a tar at all")
        (self.staging / (path.name + ".sha256")).write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n")
        code, rec = self.ask("1.0.0", sha)
        self.assertIn("not a whole gzip tar", rec["reason"])

    def test_a_pack_modified_in_the_future_is_refused(self):
        sha = self.commit("1.0.0")
        path = self.pack("1.0.0", sha)
        later = time.time_ns() + 3600 * 10 ** 9
        os.utime(path, ns=(later, later))
        code, rec = self.ask("1.0.0", sha)
        self.assertIn("modified in the future", rec["reason"])

    def test_a_symbolic_link_in_staging_is_refused(self):
        sha = self.commit("1.0.0")
        real = self.pack("1.0.0", sha)
        moved = Path(self.tmp.name) / "elsewhere.tgz"
        os.replace(real, moved)
        try:
            os.symlink(moved, real)
        except (OSError, NotImplementedError):
            self.skipTest("this machine cannot make symbolic links")
        code, rec = self.ask("1.0.0", sha)
        self.assertIn("is a symbolic link", rec["reason"])

    def test_a_newer_request_supersedes_and_a_duplicate_is_refused(self):
        a, first = self.staged_ok("1.0.0")
        code, dup = self.ask("1.0.0", a)
        self.assertEqual((code, dup["state"]), (1, "refused"))
        self.assertIn("already waiting as " + first["id"], dup["reason"])
        b, second = self.staged_ok("1.1.0", now=NOW + timedelta(minutes=5))
        old = self.store.read(first["id"])
        self.assertEqual((old["state"], old["reason"]), ("superseded", "replaced by 1.1.0"))
        self.assertEqual(self.store.read(second["id"])["state"], "pending")

    def test_hermes_messages_request_and_deploy_are_refused_for_a_staged_repository(self):
        sha = self.commit("1.0.0")
        calls = []
        real = queue.git
        with mock.patch.object(queue, "git", lambda args, **kw: calls.append(args[0]) or real(args, **kw)):
            rec = queue.admit(self.store, self.cfg, {"type": "deploy-request", "repo": "you/game", "sha": sha}, NOW)
            self.assertEqual(rec["state"], "refused")
            self.assertIn("staged-pack repository", rec["reason"])
            self.assertIsNone(commands.request(self.store, self.cfg, "you/game", now=NOW, out=self.out.append))
            self.assertIn("stage-request", self.out[-1])
        self.assertEqual(calls, [], "nothing was fetched")

    def test_a_git_repository_is_refused_by_stage_request(self):
        sha = self.commit("1.0.0")
        code, rec = staged.request(self.store, self.cfg, "you/app", "1.0.0", sha, now=NOW)
        self.assertEqual(code, 1)
        self.assertIn("is not a staged-pack repository", rec["reason"])


class StageRequestCommandLineTest(StagedBase):
    def run_cli(self, *argv):
        with mock.patch.object(config, "check", return_value=self.cfg), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            code = cli.main(["stage-request", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_the_command_line_makes_a_pending_record(self):
        sha = self.commit("1.0.0")
        self.pack("1.0.0", sha)
        code, out, err = self.run_cli("game", "--version", "1.0.0", "--sha", sha, "--tests", "12 passed")
        self.assertEqual(code, 0, err)
        self.assertIn(": pending (you/game 1.0.0", out)

    def test_a_bad_sha_is_bad_input_and_nothing_is_read(self):
        for bad in ("xyz", "A" * 40, "a" * 39):
            with mock.patch.object(staged, "locate", side_effect=AssertionError("read")):
                code, out, err = self.run_cli("game", "--version", "1.0.0", "--sha", bad)
            self.assertEqual(code, 2, err)
            self.assertIn("--sha must be the full commit", err)
        self.assertEqual(list(self.store.queue.glob("*.json")) if self.store.queue.is_dir() else [], [])

    def test_a_path_in_the_version_string_is_refused_before_anything_is_read(self):
        sha = self.commit("1.0.0")
        self.pack("1.0.0", sha)
        outside = self.staging.parent / ("x-%s.tgz" % sha)
        shutil.copyfile(self.staging / ("1.0.0-%s.tgz" % sha), outside)
        for bad in ("../x", "1.0.0/../../x", "1.0.0/..", "..", "/etc/x", "1.0.0\\..\\x", "1.0.0 ", "1.0", "~/1.0.0"):
            with mock.patch.object(staged, "locate", side_effect=AssertionError("read")), \
                    mock.patch.object(staged, "digest", side_effect=AssertionError("read")):
                code, out, err = self.run_cli("game", "--version", bad, "--sha", sha)
            self.assertEqual(code, 2, (bad, err))
            self.assertIn("--version must look like 0.46.0", err)
        self.assertEqual(list(self.store.queue.glob("*.json")) if self.store.queue.is_dir() else [], [])
        with self.assertRaises(staged.Refused):
            staged.pack_name("../x", sha)

    def test_tests_is_one_line(self):
        sha = self.commit("1.0.0")
        code, out, err = self.run_cli("game", "--version", "1.0.0", "--sha", sha, "--tests", "ok\nrm -rf /")
        self.assertEqual(code, 2)
        self.assertIn("--tests is one line", err)

    def test_an_unknown_repository(self):
        code, out, err = self.run_cli("nope", "--version", "1.0.0", "--sha", "a" * 40)
        self.assertEqual(code, 1)
        self.assertIn("not one repository in repos.json", err)


class HoldTest(StagedBase):
    def test_the_hold_checks_the_pack_and_signs_its_hash(self):
        sha, rec = self.staged_ok()
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 0, self.out)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "approved")
        entry = [e for _, _, e in log.entries(self.store)][-1]
        self.assertEqual((entry["kind"], entry["pack"]), ("staged-pack", {"sha256": rec["pack"]["sha256"],
                                                                           "size": rec["pack"]["size"]}))
        self.assertTrue(log.check(entry))

    def test_a_pack_replaced_after_the_request_is_refused_at_the_hold(self):
        sha, rec = self.staged_ok()
        path = self.staging / rec["pack"]["name"]
        st = os.lstat(path)
        body = bytearray(path.read_bytes())
        body[-20] ^= 0xFF                          # same size, same time: only the hash can tell
        path.write_bytes(bytes(body))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "refused")
        self.assertIn("the request recorded", got["reason"])
        self.assertEqual(self.janus_requests(), [], "Janus is not asked about a changed pack")
        self.assertEqual(log.entries(self.store), [])
        self.assertEqual(self.runs(), [])

    def test_a_pack_touched_or_resized_after_the_request_is_refused(self):
        sha, rec = self.staged_ok()
        path = self.staging / rec["pack"]["name"]
        os.utime(path, ns=(time.time_ns(), time.time_ns()))
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 1)
        self.assertIn("was replaced or changed after the request", self.store.read(rec["id"])["reason"])

    def test_a_pack_and_checksum_swapped_together_are_refused(self):
        sha, rec = self.staged_ok()
        for p in list(self.staging.iterdir()):
            p.unlink()
        self.pack("1.0.0", sha, files={"index.html": "<html>other build</html>"})
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 1)
        self.assertEqual(self.store.read(rec["id"])["state"], "refused")

    def test_a_pack_newer_than_the_request_is_refused(self):
        sha, rec = self.staged_ok()
        raw = self.store.read(rec["id"])
        raw["pack"]["checked_ns"] = raw["pack"]["mtime_ns"] - 1      # the pack's time is after Zeus's look at it
        self.store.write(raw)
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 1)
        self.assertIn("is newer than the request", self.store.read(rec["id"])["reason"])

    def test_a_pack_gone_before_the_hold_is_refused(self):
        sha, rec = self.staged_ok()
        (self.staging / rec["pack"]["name"]).unlink()
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 1)
        self.assertIn("is not staged", self.store.read(rec["id"])["reason"])

    def test_a_commit_off_main_at_the_hold_is_refused(self):
        from tests.test_queue import git
        sha, rec = self.staged_ok()
        git("checkout", "-q", "--orphan", "fresh", cwd=self.remote.work)
        git("commit", "-q", "-m", "rewritten", cwd=self.remote.work)
        git("push", "-q", "-f", str(self.remote.bare), "HEAD:refs/heads/main", cwd=self.remote.work)
        self.assertEqual(self.approve(rec["id"], sha, now=NOW + timedelta(minutes=1)), 1)
        self.assertIn("is no longer on main", self.store.read(rec["id"])["reason"])


class StagedDeployTest(StagedBase):
    def test_success_path_with_a_fake_install_script(self):
        sha, rec = self.approved_pack("1.0.0")
        self.assertEqual(self.deploy(rec["id"]), 0, self.out)
        got = self.store.read(rec["id"])
        self.assertEqual((got["state"], got["deploy"]["outcome"]), ("deployed", "deployed"))
        copy = self.data / "packs" / (rec["id"] + ".tgz")
        self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), rec["pack"]["sha256"])
        line = self.installs()[0].split()
        self.assertEqual(line[:3], ["install", "1.0.0", sha])
        self.assertIn("bytes=%d" % rec["pack"]["size"], line)
        self.assertIn("files=4", line)
        self.assertIn("sha256=" + rec["pack"]["sha256"], line)
        self.assertEqual(line[-1], "pack=" + copy.as_posix(), "the install reads Zeus's copy, never the staging folder")
        st = self.state()
        self.assertEqual((st["deployed"]["sha"], st["deployed"]["version"], st["deployed"]["pack"]),
                         (sha, "1.0.0", rec["pack"]["sha256"]))
        text = self.runlog(rec["id"])
        self.assertIn("pack checked: ", text)
        self.assertIn("running the install script", text)
        self.assertIn("ZEUS_STAGE=install", text)
        self.assertIn("health: ok", text)
        self.assertFalse((self.home / "src" / "game").exists(), "a staged pack has no clone")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)

    def test_a_pack_swapped_between_the_hold_and_the_run_is_never_installed(self):
        sha, rec = self.approved_pack("1.0.0")
        path = self.staging / rec["pack"]["name"]
        st = os.lstat(path)
        body = bytearray(path.read_bytes())
        body[-20] ^= 0xFF
        path.write_bytes(bytes(body))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "failed")
        self.assertFalse(got["deploy"]["install_stage"], "a hold: nothing was changed")
        self.assertIn("the request recorded", got["reason"])
        self.assertEqual(self.installs(), [], "the install script never ran")

    def test_an_approval_signed_for_another_pack_is_not_run(self):
        sha, rec = self.approved_pack("1.0.0")
        raw = self.store.read(rec["id"])
        raw["pack"]["sha256"] = "f" * 64
        self.store.write(raw)
        self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertIn("has no signed approval", self.out[0])
        self.assertEqual(self.installs(), [])

    def test_an_install_script_that_became_writable_is_not_run(self):
        sha, rec = self.approved_pack("1.0.0")
        with mock.patch.object(config, "install_problems", return_value=["is writable by group or others."]):
            self.assertEqual(self.deploy(rec["id"]), 1)
        self.assertIn("is writable by group or others", self.store.read(rec["id"])["reason"])
        self.assertEqual(self.installs(), [])

    def test_a_failure_before_the_stage_line_holds(self):
        sha, rec = self.approved_pack("1.0.0")
        with mock.patch.dict(os.environ, {"FAKE_INSTALL": "fail-before"}):
            self.assertEqual(self.deploy(rec["id"]), 1)
        got = self.store.read(rec["id"])
        self.assertEqual(got["state"], "failed")
        self.assertIn("before the install began, so nothing was changed", got["reason"])
        self.assertEqual([l.split()[0] for l in self.installs()], ["install"], "no rollback after a hold")

    def test_a_failure_after_the_stage_line_puts_the_previous_back_once(self):
        a, first = self.approved_pack("1.0.0")
        self.assertEqual(self.deploy(first["id"]), 0, self.out)
        b, second = self.approved_pack("1.1.0", now=NOW + timedelta(minutes=10))
        with mock.patch.dict(os.environ, {"FAKE_INSTALL": "fail-after"}):
            self.assertEqual(self.deploy(second["id"]), 1)
        got = self.store.read(second["id"])
        self.assertEqual(got["state"], "rolled-back")
        self.assertEqual(got["deploy"]["restored"], {"sha": a, "version": "1.0.0"})
        self.assertEqual(self.installs()[-1].split()[:4], ["rollback", "1.0.0", a, "prev=" + b])
        self.assertEqual(self.state()["deployed"]["sha"], a)

    def test_rollback_restores_the_previous_set_aside_copy(self):
        self.use_example()
        a, first = self.approved_pack("1.0.0")
        self.assertEqual(self.deploy(first["id"]), 0, self.out)
        self.assertEqual((self.live_dir / "VERSION").read_text().strip(), "1.0.0")
        b, second = self.approved_pack("1.1.0", now=NOW + timedelta(minutes=10))
        self.assertEqual(self.deploy(second["id"]), 0, (self.out, self.runlog(second["id"])))
        self.assertEqual((self.live_dir / "VERSION").read_text().strip(), "1.1.0")
        asides = sorted(self.live_dir.parent.parent.glob(".set-aside-*-game"))
        self.assertEqual(len(asides), 1)
        self.assertEqual((asides[0] / "game" / "VERSION").read_text().strip(), "1.0.0")
        self.assertEqual((asides[0] / "zeus-sha").read_text().strip(), a)
        # by hand, from the Throne: Janus, a signed line, the same deploy-run
        who = approve.Caller("elaia", USER, IP)
        self.out = []
        self.assertEqual(rollback.rollback(self.store, self.cfg, "you/game", who, sha=a, now=NOW + timedelta(hours=1),
                                           out=self.out.append), 0, self.out)
        rid = [r for r in self.store.pending(("approved",)) if r["action"] == "rollback"][0]["id"]
        self.assertEqual(self.deploy(rid), 0, (self.out, self.runlog(rid)))
        self.assertEqual((self.live_dir / "VERSION").read_text().strip(), "1.0.0")
        self.assertEqual(self.state()["deployed"]["sha"], a)
        everything = [p for p in self.live_dir.parent.parent.glob(".set-aside-*-game*")]
        versions = sorted((p / "game" / "VERSION").read_text().strip() for p in everything if (p / "game").is_dir())
        self.assertEqual(versions, ["1.1.0"], "1.1.0 is set aside, never deleted")
        self.assertIn("rolled back game to 1.0.0", self.runlog(rid))

    def test_the_example_puts_the_previous_back_when_health_fails(self):
        self.use_example()
        a, first = self.approved_pack("1.0.0")
        self.assertEqual(self.deploy(first["id"]), 0, (self.out, self.runlog(first["id"])))
        b, second = self.approved_pack("1.1.0", now=NOW + timedelta(minutes=10))
        calls = []

        def health(shape, value, want):
            calls.append(want)
            return None if want == "1.0.0" else "1.1.0 never came up"
        with mock.patch.object(deploy, "health_once", health):
            self.assertEqual(self.deploy(second["id"]), 1)
        self.assertEqual(self.store.read(second["id"])["state"], "rolled-back", self.runlog(second["id"]))
        self.assertEqual((self.live_dir / "VERSION").read_text().strip(), "1.0.0")
        kept = [p for p in self.live_dir.parent.parent.glob(".set-aside-*-game*") if (p / "game").is_dir()]
        self.assertEqual(sorted((p / "game" / "VERSION").read_text().strip() for p in kept), ["1.1.0"])

    def test_the_example_refuses_a_rollback_it_has_no_copy_for_before_the_stage_line(self):
        self.use_example()
        a, first = self.approved_pack("1.0.0")
        self.assertEqual(self.deploy(first["id"]), 0, self.out)
        b, second = self.approved_pack("1.1.0", now=NOW + timedelta(minutes=10))
        self.assertEqual(self.deploy(second["id"]), 0, self.out)
        for p in self.live_dir.parent.parent.glob(".set-aside-*-game"):
            os.replace(p, p.with_name(p.name.replace(".set-aside-", "kept-")))     # as if the copy was moved away
        who = approve.Caller("elaia", USER, IP)
        self.assertEqual(rollback.rollback(self.store, self.cfg, "you/game", who, sha=a, now=NOW + timedelta(hours=1),
                                           out=self.out.append), 0, self.out)
        rid = [r for r in self.store.pending(("approved",)) if r["action"] == "rollback"][0]["id"]
        self.assertEqual(self.deploy(rid), 1)
        got = self.store.read(rid)
        self.assertEqual(got["state"], "failed")
        self.assertFalse(got["deploy"]["install_stage"])
        self.assertIn("no set-aside copy of game 1.0.0", self.runlog(rid))
        self.assertEqual((self.live_dir / "VERSION").read_text().strip(), "1.1.0", "nothing changed")


class StagedConfigTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Home(self.tmp.name)
        self.addCleanup(self.h.env.stop)
        self.staging = self.h.home / ".config" / "argus-ui" / ".staged-game"
        self.install = self.h.home / ".config" / "zeus" / "install" / "game.sh"

    def game(self, **changes):
        doc = json.loads(json.dumps(self.h.doc))
        repo = {"kind": "staged-pack", "url": "git@github.com:you/game.git", "private": True,
                "key": "~/.config/zeus/keys/game", "staging": "~/.config/argus-ui/.staged-game",
                "install": "~/.config/zeus/install/game.sh", "health": {"service": "argus-ui"}}
        for k, v in changes.items():
            if v is None:
                repo.pop(k, None)
            else:
                repo[k] = v
        doc["repos"]["you/game"] = repo
        cfg = self.h.write(doc).check()
        return cfg, "\n".join(cfg.errors), "\n".join(cfg.warnings)

    def make(self):
        self.staging.mkdir(parents=True)
        self.install.parent.mkdir(parents=True)
        shutil.copyfile(EXAMPLE_INSTALL, self.install)
        if os.name != "nt":
            os.chmod(self.staging, 0o700)
            os.chmod(self.install.parent, 0o755)
            os.chmod(self.install, 0o755)

    def test_a_staged_pack_repository_checks_and_says_what_it_does(self):
        self.make()
        cfg, errors, warnings = self.game()
        self.assertEqual(errors, "")
        repo = cfg.repos["you/game"]
        self.assertTrue(repo.staged)
        self.assertEqual((repo.staging, repo.install), (self.staging, self.install))
        self.assertIsNone(repo.path)
        sentence = [s for s in cfg.explain() if s.startswith("you/game")][0]
        self.assertIn("a pack staged in ~/.config/argus-ui/.staged-game", sentence)
        self.assertIn("bash ~/.config/zeus/install/game.sh install", sentence)

    def test_missing_staging_and_install_only_warn(self):
        cfg, errors, warnings = self.game()
        self.assertEqual(errors, "")
        self.assertIn("staging folder ~/.config/argus-ui/.staged-game does not exist yet", warnings)
        self.assertIn("install script ~/.config/zeus/install/game.sh does not exist yet", warnings)

    def test_a_clone_path_relative_paths_and_an_unknown_kind_fail(self):
        self.assertIn('"path" is for a clone', self.game(path="~/src/game")[1])
        self.assertIn('"staging" must be the absolute folder', self.game(staging="staged")[1])
        self.assertIn('"install" must be the absolute path', self.game(install="install.sh")[1])
        self.assertIn('"install" must be the absolute path', self.game(install=None)[1])
        self.assertIn('"kind" must be "git"', self.game(kind="tarball")[1])

    def test_staging_and_install_are_refused_on_a_git_repository(self):
        doc = json.loads(json.dumps(self.h.doc))
        doc["repos"]["you/hermes"]["install"] = "~/x.sh"
        cfg = self.h.write(doc).check()
        self.assertIn('"install" is only for "kind": "staged-pack"', "\n".join(cfg.errors))

    def test_an_install_script_inside_staging_fails(self):
        self.make()
        inside = self.staging / "game.sh"
        shutil.copyfile(EXAMPLE_INSTALL, inside)
        if os.name != "nt":
            os.chmod(inside, 0o755)
        errors = self.game(install="~/.config/argus-ui/.staged-game/game.sh")[1]
        self.assertIn("is inside the staging folder", errors)

    @unittest.skipIf(os.name == "nt", "file modes mean nothing on Windows")
    def test_a_world_writable_install_script_fails(self):
        self.make()
        os.chmod(self.install, 0o757)
        errors = self.game()[1]
        self.assertIn("install script ~/.config/zeus/install/game.sh is writable by group or others", errors)
        os.chmod(self.install, 0o755)
        os.chmod(self.install.parent, 0o777)
        self.assertIn("has a folder that is writable by group or others", self.game()[1])
        os.chmod(self.install.parent, 0o755)
        os.chmod(self.staging, 0o777)
        self.assertIn("staging folder ~/.config/argus-ui/.staged-game is writable by group or others", self.game()[1])
        os.chmod(self.staging, 0o700)
        with mock.patch("os.getuid", return_value=os.getuid() + 1):
            self.assertIn("is owned by another user", self.game()[1])

    def test_a_symbolic_link_as_install_script_fails(self):
        self.make()
        link = self.install.parent / "link.sh"
        try:
            os.symlink(self.install, link)
        except (OSError, NotImplementedError):
            self.skipTest("this machine cannot make symbolic links")
        self.assertIn("is a symbolic link", self.game(install="~/.config/zeus/install/link.sh")[1])


class ThroneStagedTest(unittest.TestCase):
    def test_the_plugin_shows_the_kind_and_the_staged_stages(self):
        from tests.test_throne_plugin import load_plugin
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        cfg = tmp / "repos.json"
        cfg.write_text(json.dumps({"version": 1, "repos": {"you/game": {"kind": "staged-pack"}, "you/app": {}}}))
        (tmp / "data" / "state").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"ZEUS_DATA": str(tmp / "data"), "ZEUS_CONFIG": str(cfg)}):
            p = load_plugin()
            kinds = {d["repo"]: d["kind"] for d in p.deployed_now()}
            self.assertEqual(kinds, {"you/game": "staged-pack", "you/app": "git"})
            lines = ["zeus: 2026 deploying you/game 1.0.0", "zeus: pack checked: 1.0.0-x.tgz", "zeus: running the install script"]
            got = [s["state"] for s in p.stages({"state": "deploying"}, lines)]
            self.assertEqual(got, ["done", "done", "now", "todo", "todo"])


if __name__ == "__main__":
    unittest.main()
