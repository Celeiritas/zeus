"""zeus verify-log (docs/contract.md §4; issue #7): an untouched log passes, a line with one changed byte fails and is
named, a missing sig fails, another key fails, and the journal copy can be checked with --key-file."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from zeus import init, log, queue, verify

ROOT = Path(__file__).resolve().parent.parent
OCT = datetime(2026, 10, 21, 9, 5, 0, tzinfo=timezone(timedelta(hours=-6)))
NOV = datetime(2026, 11, 2, 9, 5, 0, tzinfo=timezone(timedelta(hours=-6)))


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        self.secret = self.home / ".config" / "zeus" / "secret.env"
        self.env = {k: v for k, v in os.environ.items() if k != "SSH_CONNECTION"}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), ZEUS_DATA=str(self.data),
                        ZEUS_SECRET=str(self.secret))
        patch = mock.patch.dict(os.environ, self.env)
        patch.start()
        self.addCleanup(patch.stop)
        init.run(lambda s: None)
        self.store = queue.Store()
        for i, when in enumerate((OCT, OCT, NOV)):
            log.append(self.store, {"t": queue.stamp(when), "id": "2026-req-%d" % i, "action": "deploy",
                                    "repo": "you/app", "sha": "%d" % i * 40, "version": "4.9.%d" % i,
                                    "by": {"user": "owner", "via": "ssh"}, "janus": "allowed", "zeus": "0.2.2"}, when=when)
        self.oct = self.data / "log" / "approvals-2026-10.jsonl"
        self.nov = self.data / "log" / "approvals-2026-11.jsonl"

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "zeus", "verify-log", *args], cwd=ROOT, capture_output=True,
                              text=True, env=self.env)

    def key(self):
        return bytes.fromhex(init.read_key())

    def test_an_untouched_log_passes(self):
        rows = verify.verify(verify.files(self.data / "log"), self.key())
        self.assertEqual([(p.name, n, v) for p, n, v, _ in rows],
                         [("approvals-2026-10.jsonl", 1, "ok"), ("approvals-2026-10.jsonl", 2, "ok"),
                          ("approvals-2026-11.jsonl", 1, "ok")])
        r = self.run_cli()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("approvals-2026-10.jsonl:1: ok (deploy 2026-req-0 0000000)", r.stdout)
        self.assertTrue(r.stdout.rstrip().endswith("zeus verify-log: 3 lines in 2 files, 3 ok, 0 bad"), r.stdout)

    def test_a_line_with_one_changed_byte_fails_and_is_named(self):
        lines = self.oct.read_text().splitlines()
        lines[1] = lines[1].replace('"4.9.1"', '"4.9.9"')
        self.oct.write_text("\n".join(lines) + "\n")
        r = self.run_cli()
        self.assertEqual(r.returncode, 10)
        self.assertIn("approvals-2026-10.jsonl:2: bad (sig does not match", r.stdout)
        self.assertIn("approvals-2026-10.jsonl:1: ok", r.stdout)
        self.assertIn("3 lines in 2 files, 2 ok, 1 bad", r.stdout)

    def test_a_changed_sig_byte_fails_too(self):
        text = self.nov.read_text()
        entry = json.loads(text)
        entry["sig"] = ("0" if entry["sig"][0] != "0" else "1") + entry["sig"][1:]
        self.nov.write_text(json.dumps(entry, sort_keys=True) + "\n")
        rows = verify.verify([self.nov], self.key())
        self.assertEqual(rows[0][2], "bad")

    def test_a_missing_sig_fails(self):
        entry = json.loads(self.nov.read_text())
        del entry["sig"]
        self.nov.write_text(json.dumps(entry) + "\n")
        r = self.run_cli()
        self.assertEqual(r.returncode, 10)
        self.assertIn("approvals-2026-11.jsonl:1: bad (no sig)", r.stdout)

    def test_broken_lines_and_another_key_fail(self):
        with open(self.nov, "a") as f:
            f.write("{ not json\n[1, 2]\n\n")
        rows = verify.verify([self.nov], self.key())
        self.assertEqual([(n, v, why) for _, n, v, why in rows][1:], [(2, "bad", "not JSON"), (3, "bad", "not a JSON object")])
        self.assertEqual({v for _, _, v, _ in verify.verify([self.oct], bytes(32))}, {"bad"})

    def test_quiet_prints_only_the_bad_lines_and_the_total(self):
        r = self.run_cli("--quiet")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "zeus verify-log: 3 lines in 2 files, 3 ok, 0 bad")

    def test_the_journal_copy_with_key_file(self):
        journal = Path(self.tmp.name) / "realm" / "zeus" / "approvals"
        journal.mkdir(parents=True)
        shutil.copy(self.oct, journal)
        key_file = Path(self.tmp.name) / "elsewhere" / "secret.env"
        key_file.parent.mkdir()
        shutil.copy(self.secret, key_file)
        os.chmod(key_file, 0o600)
        self.secret.unlink()
        r = self.run_cli(str(journal), "--key-file", str(key_file))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("2 lines in 1 file, 2 ok, 0 bad", r.stdout)
        r = self.run_cli(str(journal / "approvals-2026-10.jsonl"), "--key-file", str(key_file))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_no_key_and_no_such_path(self):
        self.secret.unlink()
        r = self.run_cli()
        self.assertEqual(r.returncode, 1)
        self.assertIn("no ZEUS_LOG_KEY", r.stderr)
        r = self.run_cli(str(self.home / "nope"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("is neither a file nor a folder", r.stderr)

    def test_an_empty_log_folder_is_zero_lines(self):
        empty = self.home / "empty"
        empty.mkdir()
        r = self.run_cli(str(empty))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("0 lines in 0 files, 0 ok, 0 bad", r.stdout)

    def test_decline_lines_verify_too(self):
        from zeus import commands
        rec = {"id": "x", "state": "pending", "repo": "you/app", "sha": "a" * 40, "version": "1", "expires": "2999-01-01T00:00:00+00:00"}
        self.store.make_folders()
        self.store.write(rec)
        self.assertEqual(commands.decline(self.store, "x", "no", now=OCT), 0)
        r = self.run_cli("--quiet")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("4 lines in 2 files, 4 ok", r.stdout)


if __name__ == "__main__":
    unittest.main()
