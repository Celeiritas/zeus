"""zeus-root: the hostile take (design 5.5 steps 1-2) and the quarantine (contract §8e, D6). Linux only."""
import json
import os
import socket
import tempfile
import time
import unittest
from unittest import mock

from tests import root_support
from zeus_root import take


class TakeCase(unittest.TestCase):
    def setUp(self):
        root_support.need("linux")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.ctx = root_support.make_ctx(tmp.name)
        self.inbox = self.ctx.run / "inbox"

    def put(self, name, data=b'{"type":"run"}'):
        (self.inbox / name).write_bytes(data)
        return self.inbox / name

    def log_lines(self):
        lines = []
        for p in sorted(self.ctx.log.glob("runs-*.jsonl")):
            lines += [json.loads(b) for b in p.read_bytes().splitlines()]
        return lines


class TakeTest(TakeCase):
    def test_every_kind_leaves_the_inbox(self):
        self.put("good")
        target = self.put("target")
        os.rename(target, os.path.join(self.tmp, "outside"))
        os.symlink(os.path.join(self.tmp, "outside"), self.inbox / "link")
        os.mkfifo(self.inbox / "fifo")
        (self.inbox / "folder").mkdir()
        s = socket.socket(socket.AF_UNIX)
        self.addCleanup(s.close)
        s.bind(str(self.inbox / "socket"))
        self.put("hardlinked")
        os.link(self.inbox / "hardlinked", os.path.join(self.tmp, "second-name"))
        self.put("big", b"x" * 16385)

        taken = take.take(self.ctx, self.inbox, "run")
        self.assertEqual((len(taken.paths), taken.left), (7, 0))
        self.assertEqual(list(self.inbox.iterdir()), [])
        got = {}
        for p in taken.paths:
            self.assertEqual(p.parent, self.ctx.run / "work")
            got[p] = take.open_entry(self.ctx, p)
        kinds = sorted((r.type, r.why) for r in got.values() if isinstance(r, take.Bad))
        self.assertEqual(kinds, [("fifo", "not a regular file"), ("file", "a hard link"), ("file", "too big"),
                                 ("folder", "not a regular file"), ("link", "a link"),
                                 ("socket", "not a regular file")])
        self.assertIn(b'{"type":"run"}', [r for r in got.values() if isinstance(r, bytes)])

    def test_fifo_does_not_block(self):
        os.mkfifo(self.inbox / "fifo")
        taken = take.take(self.ctx, self.inbox, "run")
        start = time.monotonic()
        bad = take.open_entry(self.ctx, taken.paths[0])
        self.assertLess(time.monotonic() - start, 2)
        self.assertEqual(bad.type, "fifo")

    def test_wrong_owner(self):
        p = self.put("x")
        self.ctx.gate_uid = os.getuid() + 1
        bad = take.open_entry(self.ctx, take.take(self.ctx, self.inbox, "run").paths[0])
        self.assertEqual((bad.type, bad.why, bad.uid), ("file", "wrong owner", os.getuid()))
        self.assertFalse(p.exists())

    def test_sixteen_kb_is_allowed(self):
        self.put("edge", b"y" * 16384)
        data = take.open_entry(self.ctx, take.take(self.ctx, self.inbox, "run").paths[0])
        self.assertEqual(len(data), 16384)

    def test_swapped_after_listing(self):
        """The gate swaps the name after the listing: root takes what is there at the rename and reads that, from the
        handle; the gate can't reach it any more once it is in work/."""
        self.put("x", b'{"first":1}')

        def swap(names):
            (self.inbox / "x.new").write_bytes(b'{"second":2}')
            os.rename(self.inbox / "x.new", self.inbox / "x")
        with mock.patch.object(take, "_after_listing", swap):
            taken = take.take(self.ctx, self.inbox, "run")
        self.assertEqual(take.open_entry(self.ctx, taken.paths[0]), b'{"second":2}')

    def test_sixty_entries(self):
        for i in range(60):
            self.put("e%02d" % i)
        taken = take.take(self.ctx, self.inbox, "run")
        self.assertEqual((len(taken.paths), taken.left), (50, 10))
        self.assertEqual(len(list(self.inbox.iterdir())), 10)
        self.assertEqual(len(set(taken.paths)), 50)
        self.assertEqual(taken.paths, sorted(taken.paths))  # taken in order, named in order

    def test_vanished_name_skipped(self):
        for name in ("a", "b", "c"):
            self.put(name)
        with mock.patch.object(take, "_after_listing", lambda names: os.remove(self.inbox / "b")):
            taken = take.take(self.ctx, self.inbox, "run")
        self.assertEqual(len(taken.paths), 2)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_leftovers(self):
        self.put("a")
        first = take.take(self.ctx, self.inbox, "run").paths
        self.assertEqual(take.leftovers(self.ctx), first)


class QuarantineTest(TakeCase):
    def test_quarantine_line_has_no_content(self):
        self.put("x", b"SECRET-CONTENT" * 2000)
        path = take.take(self.ctx, self.inbox, "run").paths[0]
        bad = take.open_entry(self.ctx, path)
        take.quarantine(self.ctx, path, bad)
        self.assertFalse(path.exists())
        self.assertEqual(take.quarantine_count(self.ctx), 1)
        raw = b"".join(p.read_bytes() for p in self.ctx.log.iterdir())
        self.assertNotIn(b"SECRET", raw)
        line = self.log_lines()[0]
        self.assertEqual((line["outcome"], line["class"], line["reason"], line["run"]),
                         ("refused", "other", "file-rules", None))
        self.assertEqual(set(line["entry"]), {"type", "size", "uid", "sha256_16k"})
        self.assertEqual(line["entry"]["size"], 28000)
        import hashlib
        self.assertEqual(line["entry"]["sha256_16k"], hashlib.sha256((b"SECRET-CONTENT" * 2000)[:16384]).hexdigest())
        attention = [json.loads(b) for b in (self.ctx.log / "attention.jsonl").read_bytes().splitlines()]
        self.assertEqual([a["kind"] for a in attention], ["file-rules"])

    def test_garbage_writes_no_attention_line(self):
        self.put("x", b'{"type":')
        path = take.take(self.ctx, self.inbox, "run").paths[0]
        self.assertIsInstance(take.open_entry(self.ctx, path), bytes)
        take.quarantine(self.ctx, path, None)
        line = self.log_lines()[0]
        self.assertEqual((line["class"], line["reason"], line["entry"]["type"], line["entry"]["size"]),
                         ("garbage", "malformed", "file", 8))
        self.assertFalse((self.ctx.log / "attention.jsonl").exists())

    def test_quarantine_a_link_and_a_folder(self):
        os.symlink("/etc/passwd", self.inbox / "link")
        (self.inbox / "folder").mkdir()
        for path in take.take(self.ctx, self.inbox, "run").paths:
            take.quarantine(self.ctx, path, take.open_entry(self.ctx, path))
        self.assertEqual(take.quarantine_count(self.ctx), 2)
        entries = sorted((x["entry"]["type"], x["entry"]["sha256_16k"]) for x in self.log_lines())
        self.assertEqual(entries, [("folder", None), ("link", None)])


if __name__ == "__main__":
    unittest.main()
