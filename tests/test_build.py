"""scripts/build.sh makes one runnable file (a zipapp) that needs only python3. The archive is run through
sys.executable here, so the test passes on a PC where the python3 shebang would not resolve."""
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "dist" / "zeus"
        env = dict(os.environ, PYTHON=sys.executable)
        r = subprocess.run(["bash", str(ROOT / "scripts" / "build.sh"), str(cls.out)], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stdout + r.stderr
        cls.build_output = r.stdout

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_one_file_with_a_python3_shebang(self):
        self.assertTrue(self.out.is_file())
        with open(self.out, "rb") as f:
            first = f.readline()
        self.assertEqual(first, b"#!/usr/bin/env python3\n")
        self.assertTrue(zipfile.is_zipfile(self.out))

    def test_runs_and_reports_its_version(self):
        r = subprocess.run([sys.executable, str(self.out), "--version"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "zeus " + VERSION)
        self.assertIn("zeus " + VERSION, self.build_output)

    def test_check_config_works_from_the_archive(self):
        r = subprocess.run([sys.executable, str(self.out), "check-config", "--file",
                            str(ROOT / "examples" / "repos.example.json")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("5 repositories ok", r.stdout)

    def test_archive_holds_the_package_and_the_version(self):
        with zipfile.ZipFile(self.out) as z:
            names = z.namelist()
            self.assertIn("__main__.py", names)
            self.assertIn("zeus/cli.py", names)
            self.assertIn("zeus/config.py", names)
            self.assertEqual(z.read("zeus/VERSION").decode().strip(), VERSION)
            self.assertFalse([n for n in names if "__pycache__" in n], names)

    def test_checkout_does_not_carry_a_second_version_file(self):
        self.assertFalse((ROOT / "zeus" / "VERSION").exists(), "zeus/VERSION is written by build.sh only")


if __name__ == "__main__":
    unittest.main()
