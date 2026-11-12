"""zeus-root: the run log and its hash chain (contract §8e), the result file (§8d, D5), attention lines (§8f, D7, D8)."""
import hashlib
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from tests import root_support
from zeus_root import __version__, logs

RUN = "r" + "0" * 20
OCT = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc).timestamp()
NOV = datetime(2026, 11, 2, 9, 30, tzinfo=timezone.utc).timestamp()
RESULT_KEYS = {"run", "action", "state", "sentence", "started", "updated", "deadline", "remind_at", "undo_ran",
               "undo_ok", "class", "zeus_root"}


class LogCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = root_support.make_ctx(tmp.name)

    def at(self, t):
        self.ctx.now.t = float(t)

    def lines(self, name):
        return (self.ctx.log / name).read_bytes().split(b"\n")[:-1]


class RunLogTest(LogCase):
    def test_chain_links_lines_and_months(self):
        self.at(OCT)
        logs.append_run(self.ctx, {"run": RUN, "action": "close-media-lan", "outcome": "done"})
        self.at(OCT + 60)
        logs.append_run(self.ctx, {"run": RUN, "action": "close-media-lan", "outcome": "refused"})
        self.at(NOV)
        logs.append_run(self.ctx, {"run": RUN, "action": "open-media-lan", "outcome": "done"})
        oct_lines, nov_lines = self.lines("runs-2026-10.jsonl"), self.lines("runs-2026-11.jsonl")
        self.assertEqual((len(oct_lines), len(nov_lines)), (2, 1))
        first, second, third = (json.loads(b) for b in oct_lines + nov_lines)
        self.assertIsNone(first["prev"])
        self.assertEqual(second["prev"], hashlib.sha256(oct_lines[0]).hexdigest())
        self.assertEqual(third["prev"], hashlib.sha256(oct_lines[1]).hexdigest())
        self.assertEqual(first["t"], "2026-10-15T12:00:00+00:00")
        self.assertEqual(first["zeus_root"], __version__)
        self.assertEqual(logs.chain_problems(self.ctx), [])

        path = self.ctx.log / "runs-2026-10.jsonl"
        raw = path.read_bytes()
        path.write_bytes(raw.replace(b"close-media-lan", b"close-media-lam", 1))  # one byte of line 1
        problems = logs.chain_problems(self.ctx)
        self.assertEqual(len(problems), 1)
        self.assertIn("runs-2026-10.jsonl:2", problems[0])

        path.write_bytes(raw.split(b"\n")[0] + b"\n")  # line 2 removed: November's first line no longer chains
        problems = logs.chain_problems(self.ctx)
        self.assertEqual(len(problems), 1)
        self.assertIn("runs-2026-11.jsonl:1", problems[0])

    def test_clock_stepped_back_keeps_one_chain(self):
        self.at(NOV)
        logs.append_run(self.ctx, {"run": RUN, "outcome": "done"})
        self.at(OCT)
        logs.append_run(self.ctx, {"run": RUN, "outcome": "done"})
        self.assertFalse((self.ctx.log / "runs-2026-10.jsonl").exists())
        self.assertEqual(len(self.lines("runs-2026-11.jsonl")), 2)
        self.assertEqual(logs.chain_problems(self.ctx), [])

    def test_partial_last_line_is_kept_apart(self):
        """A crash in the middle of a write leaves a partial line: the next line starts on its own line and chains to
        those exact bytes, so the only problem reported is the partial line itself."""
        self.at(NOV)
        logs.append_run(self.ctx, {"run": RUN, "outcome": "done"})
        with open(self.ctx.log / "runs-2026-11.jsonl", "ab") as f:
            f.write(b'{"t":"2026')
        logs.append_run(self.ctx, {"run": RUN, "outcome": "done"})
        lines = self.lines("runs-2026-11.jsonl")
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[2])["prev"], hashlib.sha256(b'{"t":"2026').hexdigest())
        problems = logs.chain_problems(self.ctx)
        self.assertEqual(len(problems), 1)
        self.assertIn("runs-2026-11.jsonl:2", problems[0])

    def test_no_ids_ever(self):
        for line in ({"key": {"credentialId": "x"}}, {"keys": [{"userHandle": "x"}]}, {"publicKey": "x"},
                     {"a": [[{"userId": "x"}]]}):
            with self.subTest(line=line), self.assertRaises(ValueError):
                logs.append_run(self.ctx, line)
        with self.assertRaises(ValueError):
            logs.write_result(self.ctx, RUN, state="done", credentialId="x")
        self.assertEqual(list(self.ctx.log.iterdir()), [])
        self.assertEqual(list((self.ctx.run / "results").iterdir()), [])
        logs.assert_no_ids({"key": {"ref": "0123456789abcdef"}, "assertion": {"signature": "x"}})


class ResultTest(LogCase):
    def read(self, run=RUN):
        return json.loads((self.ctx.run / "results" / (run + ".json")).read_bytes())

    def test_result_has_every_key(self):
        self.at(NOV)
        logs.write_result(self.ctx, RUN, action="close-media-lan", state="running")
        r = self.read()
        self.assertEqual(set(r), RESULT_KEYS)
        self.assertEqual((r["run"], r["state"], r["zeus_root"]), (RUN, "running", __version__))
        self.assertEqual(r["started"], "2026-11-02T09:30:00+00:00")
        self.assertEqual(r["updated"], r["started"])
        for k in ("sentence", "deadline", "remind_at", "undo_ran", "undo_ok", "class"):
            self.assertIsNone(r[k], k)

    def test_result_moves_on_and_keeps_its_start(self):
        self.at(NOV)
        logs.write_result(self.ctx, RUN, action="close-media-lan", state="running")
        self.at(NOV + 5)
        self.assertTrue(logs.write_result(self.ctx, RUN, state="failed", undo_ran=True, undo_ok=True,
                                          sentence="Close the media ports failed; it was put back"))
        r = self.read()
        self.assertEqual((r["state"], r["action"], r["undo_ok"]), ("failed", "close-media-lan", True))
        self.assertEqual(r["started"], "2026-11-02T09:30:00+00:00")
        self.assertEqual(r["updated"], "2026-11-02T09:30:05+00:00")

    def test_refused_never_overwrites(self):
        """Review Focus 1: a replayed record has the same run id; its refusal must never replace the first result."""
        logs.write_result(self.ctx, RUN, action="close-media-lan", state="done")
        before = (self.ctx.run / "results" / (RUN + ".json")).read_bytes()
        self.assertFalse(logs.write_result(self.ctx, RUN, state="refused", sentence="used nonce", **{"class": "not-counted"}))
        self.assertEqual((self.ctx.run / "results" / (RUN + ".json")).read_bytes(), before)
        other = "r" + "1" * 20
        self.assertTrue(logs.write_result(self.ctx, other, state="refused", sentence="expired", **{"class": "not-counted"}))
        self.assertEqual(self.read(other)["class"], "not-counted")

    def test_result_refuses_odd_fields(self):
        for run, fields in ((RUN, {"state": "weird"}), (RUN, {"state": "done", "class": "loud"}),
                            ("../x", {"state": "done"}), ("r" + "0" * 19, {"state": "done"}),
                            (RUN, {"state": "done", "zeus_root": "9"}), (RUN, {"state": "done", "updated": "x"}),
                            (RUN, {"action": "close-media-lan"})):
            with self.subTest(run=run, fields=fields), self.assertRaises(ValueError):
                logs.write_result(self.ctx, run, **fields)

    def test_read_result(self):
        """The runner reads a result back (a leftover's that still says running); one that does not read back is
        Broken, never guessed."""
        from zeus_root import state
        self.assertIsNone(logs.read_result(self.ctx, RUN))
        logs.write_result(self.ctx, RUN, action="close-media-lan", state="running")
        r = logs.read_result(self.ctx, RUN)
        self.assertEqual((set(r), r["state"], r["action"]), (RESULT_KEYS, "running", "close-media-lan"))
        for bad in (b'{"run":', b'[]', b'{"run":"x"}'):
            with self.subTest(bad=bad):
                (self.ctx.run / "results" / (RUN + ".json")).write_bytes(bad)
                with self.assertRaises(state.Broken):
                    logs.read_result(self.ctx, RUN)
        with self.assertRaises(ValueError):
            logs.read_result(self.ctx, "../" + RUN)


class AttentionTest(LogCase):
    def test_attention_line_keys_and_fold(self):
        self.at(NOV)
        self.assertTrue(logs.attention(self.ctx, "file-rules", "An inbox entry broke the file rules", None))
        self.at(NOV + 10)
        self.assertFalse(logs.attention(self.ctx, "file-rules", "An inbox entry broke the file rules", None))
        self.at(NOV + 3600)
        self.assertTrue(logs.attention(self.ctx, "file-rules", "An inbox entry broke the file rules", None))
        self.assertTrue(logs.attention(self.ctx, "put-back", "Close the media ports was put back", RUN))
        lines = [json.loads(b) for b in self.lines("attention.jsonl")]
        self.assertEqual(len(lines), 3)
        for line in lines:
            self.assertEqual(set(line), {"t", "run", "kind", "sentence", "folded"})
        self.assertEqual([(x["kind"], x["folded"], x["run"]) for x in lines],
                         [("file-rules", 0, None), ("file-rules", 1, None), ("put-back", 0, RUN)])

    def test_attention_refuses_unknown_kinds(self):
        for kind, run in (("loud", None), ("put-back", "../x"), ("put-back", 7)):
            with self.subTest(kind=kind, run=run), self.assertRaises(ValueError):
                logs.attention(self.ctx, kind, "x", run)
        with self.assertRaises(ValueError):
            logs.attention(self.ctx, "put-back", "", RUN)

    def test_kinds_in_buzz_order(self):
        self.assertEqual(logs.KINDS, ("copied-key", "pause", "refused-counted", "flood", "serve-changed",
                                      "list-changed", "undo-failed", "not-back", "put-back", "file-rules",
                                      "watcher-failed"))


class ModeTest(LogCase):
    def test_modes(self):
        root_support.need("linux")
        old = os.umask(0o077)
        try:
            logs.append_run(self.ctx, {"run": RUN, "outcome": "done"})
            logs.write_result(self.ctx, RUN, action="close-media-lan", state="done")
            logs.attention(self.ctx, "put-back", "Close the media ports was put back", RUN)
        finally:
            os.umask(old)
        for p in list(self.ctx.log.iterdir()) + list((self.ctx.run / "results").iterdir()):
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o644, p)


if __name__ == "__main__":
    unittest.main()
