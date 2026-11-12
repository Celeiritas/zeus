"""zeus-root: root's own state: spent nonces, counters, the pause, refusal counts, attention windows (D8), limits, the lock."""
import hashlib
import os
import tempfile
import time
import unittest
from types import SimpleNamespace

from tests import root_support
from zeus_root import state

NONCE = "A" * 43
REF = "0123456789abcdef"


class StateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = root_support.make_ctx(tmp.name)
        self.t0 = self.ctx.now.t

    def at(self, dt):
        self.ctx.now.t = self.t0 + dt


class NonceTest(StateCase):
    def test_nonce_spent_once(self):
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))
        self.assertFalse(state.spend_nonce(self.ctx, NONCE))
        self.assertTrue(state.spend_nonce(self.ctx, "B" * 42 + "A"))
        name = hashlib.sha256(NONCE.encode()).hexdigest()[:32]
        self.assertTrue((self.ctx.lib / "nonces" / name).is_file())

    def test_prune_keeps_a_day(self):
        state.spend_nonce(self.ctx, NONCE)
        self.at(86400)
        state.prune_nonces(self.ctx)
        self.assertFalse(state.spend_nonce(self.ctx, NONCE))
        self.at(86401)
        state.prune_nonces(self.ctx)
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))

    def test_kept_a_day_after_a_later_exp(self):
        """0.8.1 re-check finding 2: a nonce whose record expires later than now is kept until a day after that exp."""
        exp = int(self.t0) + 2 * 86400
        state.spend_nonce(self.ctx, NONCE, exp)
        self.at(2 * 86400 + 86400)
        state.prune_nonces(self.ctx)
        self.assertFalse(state.spend_nonce(self.ctx, NONCE))
        self.at(2 * 86400 + 86401)
        state.prune_nonces(self.ctx)
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))

    def test_exp_in_the_past_or_not_an_int_keeps_a_day(self):
        for exp in (int(self.t0) - 600, None, True, float(self.t0 + 9 * 86400), str(int(self.t0) + 9 * 86400)):
            with self.subTest(exp=exp):
                self.at(0)
                state.spend_nonce(self.ctx, NONCE, exp)
                self.at(86401)
                state.prune_nonces(self.ctx)
                self.assertTrue(state.spend_nonce(self.ctx, NONCE))
                self.at(2 * 86402)
                state.prune_nonces(self.ctx)

    def test_far_exp_is_capped_and_still_kept(self):
        """An exp up to 2^53 - 1 is a valid shape; the file time stops at 2^32 (2106), so no clock or file system
        overflows, and the nonce is still remembered until then."""
        name = hashlib.sha256(NONCE.encode()).hexdigest()[:32]
        state.spend_nonce(self.ctx, NONCE, 2 ** 53 - 1)
        self.assertEqual(os.stat(self.ctx.lib / "nonces" / name).st_mtime, state.NONCE_KEEP_LIMIT)
        self.at(30 * 365 * 86400)
        state.prune_nonces(self.ctx)
        self.assertFalse(state.spend_nonce(self.ctx, NONCE))

    def test_bad_nonce_refused(self):
        for nonce in ("A" * 42, "../" + "A" * 40, None, "A" * 42 + "B"):
            with self.subTest(nonce=nonce), self.assertRaises(ValueError):
                state.spend_nonce(self.ctx, nonce)


class CounterTest(StateCase):
    def test_counter_roundtrip(self):
        self.assertIsNone(state.get_counter(self.ctx, REF))
        state.set_counter(self.ctx, REF, 5, False)
        self.assertEqual(state.get_counter(self.ctx, REF), 5)
        state.set_counter(self.ctx, REF, 0, True)
        self.assertEqual(state.get_counter(self.ctx, REF), 0)
        self.assertIsNone(state.get_counter(self.ctx, "f" * 16))

    def test_counter_refuses_odd_values(self):
        for n, bs in ((True, False), (-1, False), (2 ** 32, False), (1.0, False), (1, 0)):
            with self.subTest(n=n, bs=bs), self.assertRaises(ValueError):
                state.set_counter(self.ctx, REF, n, bs)
        with self.assertRaises(ValueError):
            state.set_counter(self.ctx, "XYZ", 1, False)


class PauseTest(StateCase):
    def test_pause_owner_has_no_end(self):
        self.assertIsNone(state.pause_until(self.ctx))
        state.set_pause(self.ctx, None, "the owner paused root changes")
        self.at(10 ** 7)
        self.assertEqual(state.pause_until(self.ctx), "owner")
        state.clear_pause(self.ctx)
        self.assertIsNone(state.pause_until(self.ctx))
        state.clear_pause(self.ctx)  # clearing nothing is fine

    def test_timed_pause_ends(self):
        state.set_pause(self.ctx, self.t0 + 3600, "3 refusals")
        self.at(3599)
        self.assertEqual(state.pause_until(self.ctx), self.t0 + 3600)
        self.at(3600)
        self.assertIsNone(state.pause_until(self.ctx))

    def test_pause_is_never_shortened(self):
        """Only clear_pause (the owner's resume) ends or shortens a pause; a refusal's hour never cuts the owner's."""
        state.set_pause(self.ctx, None, "owner")
        self.assertFalse(state.set_pause(self.ctx, self.t0 + 3600, "3 refusals"))
        self.assertEqual(state.pause_until(self.ctx), "owner")
        state.clear_pause(self.ctx)
        self.assertTrue(state.set_pause(self.ctx, self.t0 + 3600, "3 refusals"))
        self.assertFalse(state.set_pause(self.ctx, self.t0 + 100, "shorter"))
        self.assertEqual(state.pause_until(self.ctx), self.t0 + 3600)
        self.assertTrue(state.set_pause(self.ctx, self.t0 + 7200, "longer"))
        self.assertEqual(state.pause_until(self.ctx), self.t0 + 7200)
        self.assertTrue(state.set_pause(self.ctx, None, "owner"))
        self.assertEqual(state.pause_until(self.ctx), "owner")

    def test_broken_pause_file_fails_closed(self):
        (self.ctx.lib / "pause.json").write_bytes(b'{"until":')
        with self.assertRaises(state.Broken):
            state.pause_until(self.ctx)


class RefusalTest(StateCase):
    def test_counted_refusals_window(self):
        self.assertEqual(state.count_refusal(self.ctx, "counted"), 1)
        self.at(1000)
        self.assertEqual(state.count_refusal(self.ctx, "counted"), 2)
        self.at(3599)
        self.assertEqual(state.count_refusal(self.ctx, "counted"), 3)
        self.at(3600)  # the first is now exactly an hour old: no longer counted
        self.assertEqual(state.count_refusal(self.ctx, "counted"), 3)
        self.assertEqual(state.count_refusal(self.ctx, "garbage"), 1)  # each class counts apart
        self.at(7200)  # 1000, 3599 and 3600 are all an hour old or more: only this one counts
        self.assertEqual(state.count_refusal(self.ctx, "counted"), 1)


class AttentionTest(StateCase):
    def test_attention_slot_folds(self):
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (True, 0))
        self.at(10)
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (False, 1))
        self.at(20)
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (False, 2))
        self.assertEqual(state.attention_slot(self.ctx, "watcher-failed"), (True, 0))  # its own window
        self.at(3600)
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (True, 2))
        self.at(3610)
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (False, 1))
        for _ in range(3):
            self.assertEqual(state.attention_slot(self.ctx, "put-back"), (True, 0))


class LimitTest(StateCase):
    def test_limits(self):
        entry = SimpleNamespace(id="close-media-lan", max_per_day=6, min_gap_s=120)
        self.assertIsNone(state.limit_reason(self.ctx, entry))
        state.note_run(self.ctx, entry.id)
        self.at(119)
        self.assertEqual(state.limit_reason(self.ctx, entry), "min-gap")
        self.at(120)
        self.assertIsNone(state.limit_reason(self.ctx, entry))
        for i in range(1, 6):
            self.at(i * 1000)
            self.assertIsNone(state.limit_reason(self.ctx, entry))
            state.note_run(self.ctx, entry.id)
        self.at(6000)
        self.assertEqual(state.limit_reason(self.ctx, entry), "max-per-day")
        other = SimpleNamespace(id="restart-bluetooth", max_per_day=6, min_gap_s=120)
        self.assertIsNone(state.limit_reason(self.ctx, other))
        self.at(86400)  # the run at 0 is a day old: five left in the window
        self.assertIsNone(state.limit_reason(self.ctx, entry))

    def test_long_gap_outlives_the_day_window(self):
        entry = SimpleNamespace(id="put-back-thing", max_per_day=1, min_gap_s=2 * 86400)
        state.note_run(self.ctx, entry.id)
        self.at(86400 + 1)
        self.assertEqual(state.limit_reason(self.ctx, entry), "min-gap")
        self.at(2 * 86400)
        self.assertIsNone(state.limit_reason(self.ctx, entry))


class FilesTest(StateCase):
    def test_files_are_600_and_no_temporary_left(self):
        root_support.need("linux")
        state.spend_nonce(self.ctx, NONCE)
        state.set_counter(self.ctx, REF, 1, False)
        state.set_pause(self.ctx, None, "owner")
        state.count_refusal(self.ctx, "counted")
        state.attention_slot(self.ctx, "file-rules")
        state.note_run(self.ctx, "close-media-lan")
        seen = []
        for folder, _, files in os.walk(self.ctx.lib):
            for f in files:
                p = os.path.join(folder, f)
                seen.append(f)
                self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, p)
                self.assertFalse(f.startswith("."), p)
        self.assertEqual(len(seen), 6)

    def test_lock_busy(self):
        root_support.need("linux")
        self.ctx.lock_wait_s = 0.2
        with state.lock(self.ctx):
            start = time.monotonic()
            with self.assertRaises(state.Busy):
                with state.lock(self.ctx):
                    pass
            self.assertLess(time.monotonic() - start, 2)
        with state.lock(self.ctx):
            pass


if __name__ == "__main__":
    unittest.main()
