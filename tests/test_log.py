"""The signed log (docs/contract.md §4): canonical bytes, sign, check, append and read back; no key means no line."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from zeus import init, log, queue

NOW = datetime(2026, 10, 21, 9, 5, 0, tzinfo=timezone(timedelta(hours=-6)))


class LogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        self.secret = self.home / ".config" / "zeus" / "secret.env"
        env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                           "ZEUS_DATA": str(self.data), "ZEUS_SECRET": str(self.secret)})
        env.start()
        self.addCleanup(env.stop)
        self.store = queue.Store()
        self.entry = {"t": "2026-10-21T09:05:00-06:00", "id": "x", "action": "decline", "repo": "you/app",
                      "by": {"user": "me", "via": "ssh"}, "reason": "not-now", "zeus": "0.1.4"}

    def test_canonical_is_sorted_compact_and_without_sig(self):
        self.assertEqual(log.canonical({"b": 1, "a": {"d": "é", "c": [1, 2]}, "sig": "zzz"}),
                         '{"a":{"c":[1,2],"d":"é"},"b":1}'.encode("utf-8"))

    def test_sign_and_check_with_a_given_key(self):
        k = bytes(32)
        signed = log.sign(self.entry, k)
        self.assertRegex(signed["sig"], r"^[0-9a-f]{64}$")
        self.assertTrue(log.check(signed, k))
        self.assertFalse(log.check(dict(signed, reason="later-edit"), k))
        self.assertFalse(log.check(dict(signed, sig="0" * 64), k))
        self.assertFalse(log.check(dict(self.entry), k), "no sig at all")
        self.assertNotEqual(signed["sig"], log.sign(self.entry, bytes([1]) * 32)["sig"])

    def test_the_signature_is_the_documented_hmac(self):
        import hashlib, hmac
        k = bytes.fromhex("ab" * 32)
        want = hmac.new(k, json.dumps(self.entry, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
        self.assertEqual(log.sign(self.entry, k)["sig"], want)

    def test_append_needs_the_key_from_init(self):
        with self.assertRaises(log.LogError) as cm:
            log.append(self.store, self.entry, when=NOW)
        self.assertIn("run `zeus init`", str(cm.exception))
        self.assertFalse((self.data / "log").exists())

    def test_append_writes_one_signed_line_per_decision_in_the_monthly_file(self):
        init.run(lambda s: None)
        first = log.append(self.store, self.entry, when=NOW)
        second = log.append(self.store, dict(self.entry, id="y"), when=NOW)
        p = self.data / "log" / "approvals-2026-10.jsonl"
        lines = p.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0]), first)
        self.assertTrue(log.check(first))
        self.assertTrue(log.check(second))
        self.assertEqual([e["id"] for _, _, e in log.entries(self.store)], ["x", "y"])
        self.assertEqual(lines[0], json.dumps(first, ensure_ascii=False, sort_keys=True))

    def test_entries_mark_a_broken_line(self):
        init.run(lambda s: None)
        log.append(self.store, self.entry, when=NOW)
        p = self.data / "log" / "approvals-2026-10.jsonl"
        with open(p, "a") as f:
            f.write("{ broken\n")
        rows = log.entries(self.store)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[1][2])
        self.assertEqual(rows[1][1], 2)

    def test_a_secret_with_a_non_hex_key_is_refused(self):
        self.secret.parent.mkdir(parents=True)
        self.secret.write_text("ZEUS_LOG_KEY=not-hex\n")
        with self.assertRaises(log.LogError) as cm:
            log.key()
        self.assertIn("is not hex", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
