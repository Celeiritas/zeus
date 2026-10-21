"""zeus init: the signing key in secret.env (mode 600, never overwritten) and the data folders (docs/design.md §2, §4)."""
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zeus import init

ROOT = Path(__file__).resolve().parent.parent


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.secret = self.home / ".config" / "zeus" / "secret.env"
        self.data = self.home / ".local" / "share" / "zeus"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                           "ZEUS_SECRET": str(self.secret), "ZEUS_DATA": str(self.data)})
        env.start()
        self.addCleanup(env.stop)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "zeus", *args], cwd=ROOT, capture_output=True, text=True,
                              env=dict(os.environ))

    def test_makes_the_secret_with_32_random_bytes_as_hex(self):
        out = []
        self.assertEqual(init.run(out.append), 0)
        self.assertTrue(self.secret.is_file())
        key = init.read_key()
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertIn("ZEUS_LOG_KEY=" + key, self.secret.read_text())
        self.assertIn("made (mode 600)", out[0])
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)

    def test_never_overwrites_an_existing_secret(self):
        init.run(lambda s: None)
        before = self.secret.read_text()
        out = []
        self.assertEqual(init.run(out.append), 0)
        self.assertEqual(self.secret.read_text(), before)
        self.assertIn("already exists, kept", out[0])

    def test_two_inits_make_different_keys(self):
        init.run(lambda s: None)
        first = init.read_key()
        self.secret.unlink()
        init.run(lambda s: None)
        self.assertNotEqual(first, init.read_key())

    def test_a_secret_without_the_key_line_is_reported_not_replaced(self):
        self.secret.parent.mkdir(parents=True)
        self.secret.write_text("# empty\n")
        out = []
        self.assertEqual(init.run(out.append), 1)
        self.assertEqual(self.secret.read_text(), "# empty\n")
        self.assertIn("has no ZEUS_LOG_KEY line", out[1])
        self.assertIsNone(init.read_key())

    @unittest.skipIf(os.name == "nt", "file modes mean nothing on Windows")
    def test_a_secret_other_users_can_read_is_refused_and_init_tightens_it(self):
        init.run(lambda s: None)
        os.chmod(self.secret, 0o644)
        with self.assertRaises(init.SecretError) as cm:
            init.read_key()
        self.assertIn("readable by other users", str(cm.exception))
        self.assertIn("chmod 600 " + str(self.secret), str(cm.exception))
        out = []
        self.assertEqual(init.run(out.append), 0)
        self.assertIn("already exists, kept; mode fixed to 600", out[0])
        self.assertEqual(stat.S_IMODE(self.secret.stat().st_mode), 0o600)
        self.assertRegex(init.read_key(), r"^[0-9a-f]{64}$")

    def test_makes_the_data_folders_once(self):
        out = []
        init.run(out.append)
        for name in ("requests", "queue", "queue/.done", "state", "log"):
            self.assertTrue((self.data / name).is_dir(), name)
        self.assertIn("made requests, queue, queue/.done, state, log", out[1])
        out = []
        init.run(out.append)
        self.assertIn("folders already there", out[1])

    def test_the_command_line(self):
        r = self.run_cli("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("secret.env: made (mode 600)", r.stdout)
        r = self.run_cli("init")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("already exists, kept", r.stdout)

    def test_nothing_of_the_secret_is_committed(self):
        ignored = (ROOT / ".gitignore").read_text()
        for line in ("secret.env", "queue/", "state/"):
            self.assertRegex(ignored, r"(?m)^" + re.escape(line) + r"$")


if __name__ == "__main__":
    unittest.main()
