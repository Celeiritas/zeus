"""zeus-root: root's own state: spent nonces, counters, the pause, refusal counts, attention windows (D8), limits, the lock."""
import contextlib
import errno
import hashlib
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from tests import root_support
from zeus_root import state

NONCE = "A" * 43
REF = "0123456789abcdef"


class _NotQuiteInt(int):
    """An int subclass, as bool is, but with a later value: it counts as no exp, like every type but int."""


class _Steps:
    """Stands in for a module state uses: every attribute read is one step and calls tick() before it hands the real
    one back, so a test can run something else between any two steps of a call."""

    def __init__(self, real, tick):
        self._real, self._tick = real, tick

    def __getattr__(self, name):
        self._tick()
        return getattr(self._real, name)


class _Posix:
    """Stands in for os in state with os.name "posix", so the nonce lock takes its flock on Windows too."""
    name = "posix"

    def __getattr__(self, name):
        return getattr(os, name)


class StateCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = root_support.make_ctx(tmp.name)
        self.t0 = self.ctx.now.t

    def at(self, dt):
        self.ctx.now.t = self.t0 + dt


class NonceTest(StateCase):
    def nonce_file(self):
        return self.ctx.lib / "nonces" / hashlib.sha256(NONCE.encode()).hexdigest()[:32]

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
        self.assertTrue(self.nonce_file().exists())
        self.at(86401)
        state.prune_nonces(self.ctx)
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))

    def test_kept_a_day_after_a_later_exp(self):
        """0.8.1 re-check finding 2: a nonce whose record expires later than now is kept until a day after that exp."""
        exp = int(self.t0) + 2 * 86400
        state.spend_nonce(self.ctx, NONCE, exp)
        self.at(2 * 86400 + 86400)
        state.prune_nonces(self.ctx)
        self.assertTrue(self.nonce_file().exists())
        self.at(2 * 86400 + 86401)
        state.prune_nonces(self.ctx)
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))

    def test_exp_in_the_past_or_not_an_int_keeps_a_day(self):
        for exp in (int(self.t0) - 600, None, _NotQuiteInt(int(self.t0) + 9 * 86400), float(self.t0 + 9 * 86400),
                    str(int(self.t0) + 9 * 86400)):
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

    def test_a_later_signed_exp_keeps_a_spent_nonce_longer(self):
        """Re-check of de22580, finding 1: a nonce spent before any signature checked out (exp None, a spoiled copy)
        is kept until a day after a signed exp that comes later (the intact record refused for its far exp), so the
        intact record cannot run when that exp comes near."""
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))
        exp = int(self.t0) + 2 * 86400
        self.at(3600)
        self.assertFalse(state.spend_nonce(self.ctx, NONCE, exp))
        self.at(2 * 86400 + 86400)
        state.prune_nonces(self.ctx)
        self.assertTrue(self.nonce_file().exists())
        self.at(2 * 86400 + 86401)
        state.prune_nonces(self.ctx)
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))

    def test_every_spend_moves_a_spent_nonce_forward_never_back(self):
        """Re-check of 1f44350, N1: a spend of a nonce already spent keeps it at least a day from that spend, with or
        without an exp, and only an int exp later than now moves it past now. Nothing moves its time back: not an
        earlier exp, not a clock that went back."""
        far = int(self.t0) + 9 * 86400
        for first, kept in ((None, self.t0 + 3600), (far, far)):
            with self.subTest(first=first):
                for f in (self.ctx.lib / "nonces").iterdir():
                    f.unlink()
                self.at(0)
                self.assertTrue(state.spend_nonce(self.ctx, NONCE, first))
                self.at(3600)
                for exp in (None, _NotQuiteInt(far + 86400), int(self.t0) - 600, int(self.t0) + 3000,
                            float(far + 86400), str(far + 86400)):
                    self.assertFalse(state.spend_nonce(self.ctx, NONCE, exp))
                    self.assertEqual(os.stat(self.nonce_file()).st_mtime, kept, exp)
                self.at(1800)                       # the clock went back
                self.assertFalse(state.spend_nonce(self.ctx, NONCE))
                self.assertEqual(os.stat(self.nonce_file()).st_mtime, kept)
                self.at(3600)
                self.assertFalse(state.spend_nonce(self.ctx, NONCE, far - 86400))
                self.assertEqual(os.stat(self.nonce_file()).st_mtime, max(kept, far - 86400))

    def test_a_spend_keeps_a_reused_nonce_a_day_from_that_spend(self):
        """Re-check of 1f44350, N1: a lying gate re-issues a nonce root spent almost a day ago. The copy refused now
        (spoiled before its signature, or busy at the lock: no exp) keeps the nonce a day from now, so the intact
        record, inside its 180 s, still finds it spent after the next start's prune."""
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))
        self.at(86400 - 30)
        self.assertFalse(state.spend_nonce(self.ctx, NONCE))
        self.at(86400 + 150)
        state.prune_nonces(self.ctx)
        self.assertFalse(state.spend_nonce(self.ctx, NONCE))
        self.at(2 * 86400 + 151)
        state.prune_nonces(self.ctx)
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))

    def test_a_far_signed_exp_moves_a_spent_nonce_only_to_the_cap(self):
        """Re-check of 32b460d, note 4: moving a spent nonce's time stops at 2^32 too, as a first spend does."""
        path = self.ctx.lib / "nonces" / hashlib.sha256(NONCE.encode()).hexdigest()[:32]
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))
        self.at(3600)
        self.assertFalse(state.spend_nonce(self.ctx, NONCE, 2 ** 53 - 1))
        self.assertEqual(os.stat(path).st_mtime, state.NONCE_KEEP_LIMIT)

    def test_every_change_to_the_nonce_store_holds_the_nonce_lock(self):
        """Re-check of 1f44350, N1: every spend (with or without an exp, first or not; the runner's spend for a record
        refused busy at lib/lock among them) and every prune touch the nonce folder only while they hold the nonce
        lock, so a spend can move a kept nonce forward and nothing moves it back between its fstat and its stamp."""
        inside, steps = [False], [0]

        @contextlib.contextmanager
        def held(ctx):
            self.assertIs(ctx, self.ctx)
            self.assertFalse(inside[0])
            inside[0] = True
            try:
                yield
            finally:
                inside[0] = False

        def tick():
            steps[0] += 1
            self.assertTrue(inside[0], "a step on the nonce store outside the nonce lock")
        with mock.patch.object(state, "_nonce_lock", held), mock.patch.object(state, "os", _Steps(os, tick)), \
                mock.patch.object(state, "tempfile", _Steps(tempfile, tick)):
            self.assertTrue(state.spend_nonce(self.ctx, NONCE))
            self.assertFalse(state.spend_nonce(self.ctx, NONCE))
            self.at(3600)
            self.assertFalse(state.spend_nonce(self.ctx, NONCE, int(self.t0) + 86400))
            self.at(3 * 86400)
            state.prune_nonces(self.ctx)
        self.assertGreater(steps[0], 20)
        self.assertFalse(self.nonce_file().exists())

    def test_a_spend_or_prune_waits_for_the_nonce_lock_then_says_busy(self):
        """The nonce lock is a flock of its own: held elsewhere (another start's spend), a spend or a prune waits
        ctx.lock_wait_s, raises Busy and leaves the folder as it was; lib/lock held (a start) does not block it."""
        root_support.need("linux")
        import fcntl
        self.ctx.lock_wait_s = 0.2
        fd = os.open(self.ctx.lib / state.NONCE_LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            for call in (lambda: state.spend_nonce(self.ctx, NONCE), lambda: state.prune_nonces(self.ctx)):
                start = time.monotonic()
                with self.assertRaises(state.NonceBusy):
                    call()
                self.assertLess(time.monotonic() - start, 2)
            self.assertEqual(list((self.ctx.lib / "nonces").iterdir()), [])
        finally:
            os.close(fd)
        with state.lock(self.ctx):
            self.assertTrue(state.spend_nonce(self.ctx, NONCE))
            state.prune_nonces(self.ctx)

    def test_the_folder_is_fsynced_before_every_answer(self):
        """Re-check of 1f44350, N3: the nonces folder is fsynced before spend answers, False too, so the refusal root
        writes after a False cannot outlive, after a power cut, the nonce file that False relied on."""
        seen = []
        with mock.patch.object(state, "_fsync_dir", seen.append):
            self.assertTrue(state.spend_nonce(self.ctx, NONCE))
            self.assertFalse(state.spend_nonce(self.ctx, NONCE))
            self.assertFalse(state.spend_nonce(self.ctx, NONCE, int(self.t0) + 86400))
        self.assertEqual(seen, [self.ctx.lib / "nonces"] * 3)

    def test_a_folder_at_a_nonce_path_fails_closed(self):
        """Re-check of 1f44350, N2: something that is not a regular file at a nonce's path makes every spend raise,
        never answer. Only root writes this folder, so this is robustness, not a gate's attack. It raises Broken, as
        spend_nonce says, not the OSError of the open (re-check of 8704c4f, F1)."""
        self.nonce_file().mkdir()
        for exp in (None, int(self.t0) + 86400):
            with self.subTest(exp=exp), self.assertRaises(state.Broken):
                state.spend_nonce(self.ctx, NONCE, exp)

    def test_a_socket_or_a_link_at_a_nonce_path_is_broken(self):
        """Re-check of 8704c4f, F1: a socket, a link to a regular file or a dangling link at a nonce's path makes a
        spend raise Broken, not the OSError of the open, and the file a link points to keeps its time."""
        root_support.need("linux")
        import socket
        target = self.ctx.lib / "elsewhere"
        target.write_bytes(b"")
        os.utime(target, (1, 1))
        for kind in ("socket", "link", "dangling"):
            with self.subTest(kind):
                path = self.nonce_file()
                if kind == "socket":
                    s = socket.socket(socket.AF_UNIX)
                    self.addCleanup(s.close)
                    s.bind(str(path))
                else:
                    os.symlink(target if kind == "link" else self.ctx.lib / "missing", path)
                for exp in (None, int(self.t0) + 86400):
                    with self.assertRaises(state.Broken):
                        state.spend_nonce(self.ctx, NONCE, exp)
                os.unlink(path)
        self.assertEqual(os.stat(target).st_mtime, 1)

    def test_another_error_opening_a_spent_nonce_is_raised_as_it_is(self):
        """Re-check of 8704c4f, F1: an open of a spent nonce's regular file that fails for another reason (out of file
        handles) raises that OSError, not Broken: Broken says the store holds something root never wrote there."""
        self.assertTrue(state.spend_nonce(self.ctx, NONCE))
        real, path = os.open, str(self.nonce_file())

        def opener(p, *a, **kw):
            if str(p) == path:
                raise OSError(errno.EMFILE, "too many open files")
            return real(p, *a, **kw)
        with mock.patch.object(state.os, "open", opener), self.assertRaises(OSError) as cm:
            state.spend_nonce(self.ctx, NONCE)
        self.assertEqual(cm.exception.errno, errno.EMFILE)

    def test_only_the_nonce_lock_says_nonce_busy(self):
        """Re-check of 8704c4f, F2: a nonce lock that is not free makes a spend and a prune raise NonceBusy, a Busy;
        lib/lock raises a plain Busy. So the runner can stop on any Busy and still tell a spend that could not run from
        a record it refused busy at the lock (Z2b). A stand-in flock that is never free, so this runs on Windows too."""
        def never_free(fd, op):
            raise BlockingIOError
        self.ctx.lock_wait_s = 0
        self.assertTrue(issubclass(state.NonceBusy, state.Busy))
        with mock.patch.dict("sys.modules", {"fcntl": SimpleNamespace(LOCK_EX=2, LOCK_NB=4, flock=never_free)}), \
                mock.patch.object(state, "os", _Posix()):
            for call in (lambda: state.spend_nonce(self.ctx, NONCE), lambda: state.prune_nonces(self.ctx)):
                with self.subTest(call=call), self.assertRaises(state.NonceBusy):
                    call()
            with self.assertRaises(state.Busy) as cm:
                with state.lock(self.ctx):
                    pass
            self.assertNotIsInstance(cm.exception, state.NonceBusy)
        self.assertEqual(list((self.ctx.lib / "nonces").iterdir()), [])

    def test_a_fifo_at_a_nonce_path_never_blocks_a_spend(self):
        """Re-check of 1f44350, N2: a FIFO at a nonce's path, with or without a reader, makes a spend raise at once:
        the nonce file is opened without blocking and checked to be a regular file (design 16.4 step 6 opens inbox
        entries the same way). Blocking there would hold both locks for ever and stop every root action."""
        root_support.need("linux")
        import signal

        def stuck(*_):
            raise AssertionError("spend blocked on a FIFO")
        old = signal.signal(signal.SIGALRM, stuck)
        self.addCleanup(signal.signal, signal.SIGALRM, old)
        self.addCleanup(signal.alarm, 0)
        os.mkfifo(self.nonce_file(), 0o600)
        for reader in (False, True):
            r = os.open(self.nonce_file(), os.O_RDONLY | os.O_NONBLOCK) if reader else None
            try:
                for exp in (None, int(self.t0) + 86400):
                    with self.subTest(reader=reader, exp=exp):
                        signal.alarm(5)
                        with self.assertRaises(state.Broken):
                            state.spend_nonce(self.ctx, NONCE, exp)
                        signal.alarm(0)
            finally:
                if r is not None:
                    os.close(r)

    def test_file_time_is_set_before_its_fsync(self):
        """Re-check of f4e8ad8, note C: the nonce file gets its time on the open file before the file's fsync, so a
        crash cannot leave it with only its creation time (kept a day instead of until its exp), also when a later
        signed exp moves it. Only fsyncs of the nonce file itself count: a second spend fsyncs a temporary too, which
        is never linked. Linux only: Windows has no utime on a file descriptor, so there the time is set by name after
        the close."""
        root_support.need("linux")
        import stat
        from unittest import mock
        real, seen = os.fsync, []

        def fsync(fd):
            st = os.fstat(fd)
            if stat.S_ISREG(st.st_mode):
                seen.append((st.st_ino, st.st_mtime))
            real(fd)
        exp = int(self.t0) + 3 * 86400     # an int, as a signed exp is: a float exp counts as none
        with mock.patch.object(state.os, "fsync", fsync):
            self.assertTrue(state.spend_nonce(self.ctx, NONCE, exp))
            self.at(3600)
            self.assertFalse(state.spend_nonce(self.ctx, NONCE, exp + 86400))
        ino = os.stat(self.ctx.lib / "nonces" / hashlib.sha256(NONCE.encode()).hexdigest()[:32]).st_ino
        self.assertEqual([t for i, t in seen if i == ino], [exp, exp + 86400])

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
        for exp in (1.5, True, "1000"):
            with self.subTest(exp=exp), self.assertRaises(ValueError):
                state.set_counter(self.ctx, REF, 1, False, exp)

    def test_key_state_keeps_the_last_signed_exp_and_never_moves_it_back(self):
        """Plan v6, item 13: the exp of the last record accepted with a key is kept with its counter, so an older one
        is refused before the counter check; a smaller exp or none never replaces a larger one."""
        self.assertEqual(state.key_state(self.ctx, REF), (None, None))
        state.set_counter(self.ctx, REF, 1, True, 1000)
        self.assertEqual(state.key_state(self.ctx, REF), (1, 1000))
        state.set_counter(self.ctx, REF, 2, True, 900)
        self.assertEqual(state.key_state(self.ctx, REF), (2, 1000))
        state.set_counter(self.ctx, REF, 3, True)
        self.assertEqual(state.key_state(self.ctx, REF), (3, 1000))
        state.set_counter(self.ctx, REF, 4, True, 1100)
        self.assertEqual(state.key_state(self.ctx, REF), (4, 1100))
        self.assertEqual(state.get_counter(self.ctx, REF), 4)

    def test_key_state_of_an_entry_from_before_the_exp(self):
        """An entry written before 0.8.2 has no exp: None, never a guess. A wrong exp type does not read back."""
        (self.ctx.lib / "counters.json").write_bytes(b'{"%s":{"bs":true,"count":4,"t":1}}\n' % REF.encode())
        self.assertEqual(state.key_state(self.ctx, REF), (4, None))
        (self.ctx.lib / "counters.json").write_bytes(b'{"%s":{"bs":true,"count":4,"exp":"1","t":1}}\n' % REF.encode())
        with self.assertRaises(state.Broken):
            state.key_state(self.ctx, REF)


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

    def test_refusals_in_hour_only_reads(self):
        """Reading the count adds nothing to it (the tests read what a start counted)."""
        self.assertEqual(state.refusals_in_hour(self.ctx, "garbage"), 0)
        state.count_refusal(self.ctx, "garbage")
        self.at(10)
        state.count_refusal(self.ctx, "garbage")
        for _ in range(3):
            self.assertEqual(state.refusals_in_hour(self.ctx, "garbage"), 2)
        self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 0)
        self.at(3600)  # the first is an hour old
        self.assertEqual(state.refusals_in_hour(self.ctx, "garbage"), 1)
        self.at(3610)
        self.assertEqual(state.refusals_in_hour(self.ctx, "garbage"), 0)


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


class ClockAheadTest(StateCase):
    """Root's clock ran a day ahead, then was set right (re-check notes, item 1). A counting window takes a time later
    than now as now and writes it back, so the correction holds it at most the window's length; a line window takes it
    as long ago, so its line is written (one too many, never none)."""
    AHEAD = 86400

    def test_the_hourly_cap_holds_an_hour_after_the_correction(self):
        self.at(self.AHEAD)
        self.assertTrue(state.open_slot(self.ctx, 2))
        self.assertTrue(state.open_slot(self.ctx, 2))
        self.at(0)
        self.assertFalse(state.open_slot(self.ctx, 2))      # the two slots count as now
        self.at(3599)
        self.assertFalse(state.open_slot(self.ctx, 2))
        self.at(3600)                                       # an hour after the correction, not a day and an hour
        self.assertTrue(state.open_slot(self.ctx, 2))

    def test_counted_refusals_hold_an_hour_after_the_correction(self):
        self.at(self.AHEAD)
        state.count_refusal(self.ctx, "counted")
        state.count_refusal(self.ctx, "counted")
        self.at(0)
        self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 2)
        self.assertEqual(state.count_refusal(self.ctx, "counted"), 3)
        self.at(3600)
        self.assertEqual(state.count_refusal(self.ctx, "counted"), 1)

    def test_action_limits_hold_a_window_after_the_correction(self):
        entry = SimpleNamespace(id="close-media-lan", max_per_day=1, min_gap_s=120)
        self.at(self.AHEAD)
        state.note_run(self.ctx, entry.id)
        self.at(0)
        self.assertEqual(state.limit_reason(self.ctx, entry), "max-per-day")    # the run counts as now
        self.at(self.AHEAD - 1)
        self.assertEqual(state.limit_reason(self.ctx, entry), "max-per-day")
        self.at(self.AHEAD)                                 # a day after the correction
        self.assertIsNone(state.limit_reason(self.ctx, entry))
        gap = SimpleNamespace(id="restart-bluetooth", max_per_day=6, min_gap_s=120)
        self.at(2 * self.AHEAD)
        state.note_run(self.ctx, gap.id)
        self.at(self.AHEAD)
        self.assertEqual(state.limit_reason(self.ctx, gap), "min-gap")
        self.at(self.AHEAD + 120)
        self.assertIsNone(state.limit_reason(self.ctx, gap))

    def test_a_flood_line_seen_ahead_does_not_hide_the_next(self):
        self.at(self.AHEAD)
        self.assertTrue(state.flood_seen(self.ctx))
        self.at(0)
        self.assertTrue(state.flood_seen(self.ctx))         # long ago, not within the hour
        self.at(10)
        self.assertFalse(state.flood_seen(self.ctx))

    def test_an_attention_window_seen_ahead_writes_the_next_line(self):
        self.at(self.AHEAD)
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (True, 0))
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (False, 1))
        self.at(0)
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (True, 1))
        self.at(10)
        self.assertEqual(state.attention_slot(self.ctx, "file-rules"), (False, 1))


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
        self.assertEqual(len(seen), 7)     # with lib/nonce-lock

    def test_lock_busy(self):
        root_support.need("linux")
        self.ctx.lock_wait_s = 0.2
        with state.lock(self.ctx):
            start = time.monotonic()
            with self.assertRaises(state.Busy) as cm:
                with state.lock(self.ctx):
                    pass
            self.assertNotIsInstance(cm.exception, state.NonceBusy)
            self.assertLess(time.monotonic() - start, 2)
        with state.lock(self.ctx):
            pass

    def test_write_lock_busy_is_its_own_kind(self):
        """lib/write-lock is apart from lib/lock: a start holding lib/lock never blocks the busy start's lines, and a
        write lock held too long raises WriteBusy, never the runner's busy refusal (a plain Busy)."""
        root_support.need("linux")
        from zeus_root import logs
        self.ctx.lock_wait_s = 0.2
        with state.lock(self.ctx):
            with state.write_lock(self.ctx):
                pass
            logs.append_run(self.ctx, {"run": None, "outcome": "refused"})
        with state.write_lock(self.ctx):
            for call in (lambda: state.write_lock(self.ctx).__enter__(),
                         lambda: logs.append_run(self.ctx, {"run": None, "outcome": "refused"}),
                         lambda: logs.attention(self.ctx, "flood", "Zeus's inbox was flooded", None)):
                with self.assertRaises(state.WriteBusy):
                    call()
            with state.lock(self.ctx):
                pass
        self.assertEqual(len(list(logs.iter_run_lines(self.ctx))), 1)
        self.assertFalse((self.ctx.log / "attention.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
