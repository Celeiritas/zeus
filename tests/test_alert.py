"""Zeus's alert file (docs/design.md §6; issue #10): Hermes's format, raise and clear, a warning never hides an alert,
`zeus seen`, and the registry entries Argus reads for the task cards."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from zeus import alert

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 21, 9, 5, 0, tzinfo=timezone(timedelta(hours=-6)))


class AlertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "state" / "argus-ui" / "alerts" / "zeus.json"
        env = mock.patch.dict(os.environ, {"ZEUS_ALERT": str(self.file)})
        env.start()
        self.addCleanup(env.stop)

    def test_raise_writes_hermes_format(self):
        doc = alert.raise_("zeus-deploy", "alert", "argus-ui 4.9.1 failed and 4.9.0 could not be restored", NOW)
        on_disk = json.loads(self.file.read_text())
        self.assertEqual(on_disk, doc)
        self.assertEqual(on_disk, {"job": "zeus-deploy", "level": "alert", "since": "2026-10-21T09:05:00-06:00",
                                   "message": "argus-ui 4.9.1 failed and 4.9.0 could not be restored", "link": "throne"})
        self.assertEqual(alert.read(), doc)

    def test_clear(self):
        self.assertFalse(alert.clear())
        alert.raise_("zeus-approve", "alert", "x", NOW)
        self.assertTrue(alert.clear())
        self.assertFalse(self.file.exists())
        self.assertIsNone(alert.read())

    def test_a_warning_never_hides_an_alert(self):
        alert.raise_("zeus-deploy", "alert", "both failed", NOW)
        up = alert.raise_("zeus-deploy", "warn", "a later build failed", NOW + timedelta(minutes=5))
        self.assertEqual(up["message"], "both failed")
        self.assertEqual(json.loads(self.file.read_text())["level"], "alert")
        up = alert.raise_("zeus-approve", "alert", "a newer alert", NOW + timedelta(minutes=6))
        self.assertEqual(json.loads(self.file.read_text())["message"], "a newer alert")
        alert.clear()
        alert.raise_("zeus-deploy", "warn", "only a warning", NOW)
        self.assertEqual(json.loads(self.file.read_text())["level"], "warn")

    def test_levels_are_fixed(self):
        with self.assertRaises(ValueError):
            alert.raise_("zeus-deploy", "panic", "x", NOW)

    def test_a_broken_file_reads_as_none(self):
        self.file.parent.mkdir(parents=True)
        self.file.write_text("{ broken")
        self.assertIsNone(alert.read())
        alert.raise_("zeus-deploy", "warn", "x", NOW)
        self.assertEqual(alert.read()["level"], "warn")

    def test_zeus_seen(self):
        env = dict(os.environ, ZEUS_ALERT=str(self.file))
        run = lambda: subprocess.run([sys.executable, "-m", "zeus", "seen"], cwd=ROOT, capture_output=True, text=True, env=env)
        r = run()
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "zeus seen: no alert was up"))
        alert.raise_("zeus-deploy", "alert", "app 4.9.1 failed and 4.9.0 could not be restored", NOW)
        r = run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), 'zeus seen: cleared the alert "app 4.9.1 failed and 4.9.0 could not be restored"')
        self.assertFalse(self.file.exists())


class RegistryTest(unittest.TestCase):
    def test_the_two_cards_have_notes_code_log_writes_to(self):
        for name in ("zeus-queue", "zeus-deploy"):
            doc = json.loads((ROOT / "units" / "agents" / (name + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(sorted(doc), ["code", "log", "notes", "writes_to"], name)
            self.assertTrue(doc["notes"].strip())
            self.assertIsInstance(doc["writes_to"], list)

    def test_install_copies_them_where_argus_reads(self):
        text = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn("units/agents/zeus-queue.json", text)
        self.assertIn(".config/argus-ui/agents", text)

    def test_install_always_builds(self):
        # a dist/zeus left from an older checkout was installed as is: LeafyBard ran 0.3.3 with 0.4.2 pulled
        lines = [l.strip() for l in (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(l.startswith("bash scripts/build.sh") for l in lines))
        self.assertFalse(any("[ -f dist/zeus ] ||" in l for l in lines))


if __name__ == "__main__":
    unittest.main()
