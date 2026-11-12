"""Root's own state under ctx.lib (/var/lib/zeus-root): spent nonces, signature counters, the pause, refusal counts,
attention windows (D8), per-action limits, and the lock every start takes.

Callers hold lock(ctx) around every read-modify-write here (the runner for a whole start, the command line for one
change). The nonce store also has a short lock of its own, which every spend and every prune takes, so the one call
without lock(ctx), the runner's spend for a record refused busy at it (Z2b), never interleaves with another. Each state
file is written to a temporary name in its own folder, flushed and renamed into place, mode 600; a new nonce file is
linked into place from its temporary name instead, so it never replaces one that is there. A state file that doesn't
read back raises Broken, and the caller stops instead of guessing (fail closed). Times come from ctx.now(); only the
locks' waits use the real monotonic clock.
"""
import contextlib
import hashlib
import math
import os
import stat
import tempfile
import time

from . import canon

HOUR_S = 3600
DAY_S = 86400
NONCE_KEEP_LIMIT = 2 ** 32             # no nonce file time past 2106-02-07 (a record expiring later is never fresh before)
NONCE_LOCK = "nonce-lock"              # lib/nonce-lock: beside lib/lock, outside nonces/ so a prune never sees it
WINDOWED = ("file-rules", "watcher-failed")   # D8: one line an hour; later ones are folded into the next line
MAX_KEPT = 100                                 # refusal times kept per class: enough for any "3 in an hour"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)


class Broken(Exception):
    """A state file root wrote doesn't read back as expected: stop and say so, never guess."""


class Busy(Exception):
    """Another start held lib/lock for longer than ctx.lock_wait_s (or, as NonceBusy, another call the nonce lock)."""


class NonceBusy(Busy):
    """Another call held the nonce lock for longer than ctx.lock_wait_s; only a spend or a prune raises it, never
    lib/lock. A Busy, so a caller that stops on any Busy still stops; but a record whose spend raised it was not refused
    busy at lib/lock (Z2b): the runner writes no result for it and does not answer it again (re-check of 8704c4f, F2)."""


def _fsync_dir(folder):
    if os.name != "posix":
        return
    fd = os.open(folder, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_file(path, data, mode=0o600, owner=None):
    """Write bytes to a temporary name beside path, flush, set the mode (and owner=(uid, gid) if given) and rename into
    place, so a reader sees the old file or the new one, never half of one, and never the new one with loose rights."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + path.name + ".")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if owner is not None and os.name == "posix":
            os.chown(tmp, *owner)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(path.parent)


def _read(ctx, name):
    path = ctx.lib / name
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        obj = canon.strict_loads(data)
    except canon.Malformed:
        raise Broken(name + " does not read back") from None
    if not isinstance(obj, dict):
        raise Broken(name + " is not an object")
    return obj


def _save(ctx, name, obj):
    write_file(ctx.lib / name, canon.canonical(obj) + b"\n")


def _number(x, name):
    if type(x) not in (int, float) or not math.isfinite(x):
        raise Broken(name + ": a time is not a number")
    return x


def _times(v, name):
    if not isinstance(v, list):
        raise Broken(name + ": not a list of times")
    return [_number(t, name) for t in v]


def _ref(ref):
    if not isinstance(ref, str) or not canon.REF_RE.fullmatch(ref):
        raise ValueError("not a key ref")
    return ref


def _action_id(action_id):
    if not isinstance(action_id, str) or not canon.ID_RE.fullmatch(action_id):
        raise ValueError("not an action id")
    return action_id


# --- nonces --------------------------------------------------------------------------------------------------------

def _nonce_path(ctx, nonce):
    if not canon.valid_nonce(nonce):
        raise canon.Malformed("bad nonce")
    return ctx.lib / "nonces" / hashlib.sha256(nonce.encode("ascii")).hexdigest()[:32]


def _stamp(path, fd, t):
    """Give the open nonce file the time t, flush it and close it. The time is set on the open file before its fsync,
    so a crash cannot leave the file with an older time (re-check of f4e8ad8, note C); Windows (tests only) has no
    utime on a file descriptor and sets it by name after the close."""
    on_fd = os.utime in os.supports_fd
    try:
        if on_fd:
            os.utime(fd, (t, t))
        os.fsync(fd)
    finally:
        os.close(fd)
    if not on_fd:
        os.utime(path, (t, t))


def spend_nonce(ctx, nonce, exp=None):
    """Mark a nonce used: True the first time, False until it is pruned. exp is the signed expiry of the nonce's record
    (unix seconds), or None (anything but an int counts as None) when there is none or it was not checked: webauthn
    passes it only once the signature checked out. Every spend keeps the nonce until at least a day after that spend,
    and a signed exp still ahead of root's clock keeps it until a day after that exp (the file time stops at 2^32); a
    nonce already spent is kept longer, never shorter. So a genuine record refused because its exp was too far ahead
    cannot run once that exp comes near (0.8.1 re-check, finding 2); a copy refused before its signature followed by
    the intact record, whose signature checks out, keeps the nonce until a day after that exp (re-check of de22580,
    finding 1); and a copy refused inside its record's 180 s keeps the nonce a day from that refusal even when the gate
    re-issued a nonce root still kept, so the intact record cannot get past it after the next prune (re-check of
    1f44350, N1). A signed exp already past moves nothing beyond now: that record is never fresh again.

    Every spend holds the nonce lock, the runner's for a record refused busy at lib/lock (Z2b) too, so no other spend
    or prune changes the file between its fstat and its stamp. A new nonce file still gets its time under a temporary
    name and is then linked into place, so a crash never leaves it without that time, and the folder is fsynced before
    any answer, False too (re-check of 1f44350, N3). Raises NonceBusy (a Busy) when the nonce lock is not free within
    ctx.lock_wait_s, Broken when something other than a regular file is at the nonce's path (a folder, a FIFO, a
    socket, a link), and any other OSError as it is (out of file handles, say): the caller writes no result for any of
    them (re-check of 8704c4f, F1 and F2)."""
    path = _nonce_path(ctx, nonce)
    with _nonce_lock(ctx):
        now = ctx.now()
        signed_later = type(exp) is int and exp > now
        keep = max(now, min(exp, NONCE_KEEP_LIMIT)) if signed_later else now
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".spend.")
        try:
            _stamp(tmp, fd, keep)
            try:
                os.link(tmp, path)
                first = True
            except FileExistsError:
                first = False
        finally:
            with contextlib.suppress(OSError):   # a temporary left behind is pruned by its time like a nonce
                os.unlink(tmp)
        if not first:
            _keep_until(path, keep)
        _fsync_dir(path.parent)
    return first


def _keep_until(path, t):
    """Move a spent nonce's file time forward to t, never back; the caller holds the nonce lock, so nothing changes the
    time between the fstat and the stamp. The file is opened without blocking and must be a regular file, else Broken:
    a FIFO there would otherwise block the spend for ever with both locks held (re-check of 1f44350, N2). An open that
    fails because something else is there (a folder, a FIFO without a reader, a socket, a link) is Broken too; any
    other OSError is raised as it is (re-check of 8704c4f, F1)."""
    try:
        fd = os.open(path, os.O_WRONLY | _NOFOLLOW | _CLOEXEC | _NONBLOCK)
    except OSError as e:
        if not _regular(path):
            raise Broken("a nonce path is not a regular file") from e
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Broken("a nonce path is not a regular file")
        later = st.st_mtime < t
    except BaseException:
        os.close(fd)
        raise
    if later:
        _stamp(path, fd, t)
    else:
        os.close(fd)


def _regular(path):
    """Whether path itself, never what a link there points to, is a regular file (False when it cannot be looked at)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def prune_nonces(ctx):
    """Forget a nonce a day after its file's time (when it was last spent, or the latest signed exp root saw for it if
    later). By then the record it was spent for expired a day ago, or (no signed exp) root last refused a copy of it
    before its signature more than a day ago, which covers any record whose exp is at most a day after that refusal. A
    record with an exp more than a day ahead that root refused only before its signature while it kept the nonce is
    the key page's to stop (contract §8b). Holds the nonce lock, like every spend."""
    with _nonce_lock(ctx):
        cutoff = ctx.now() - DAY_S
        with os.scandir(ctx.lib / "nonces") as it:
            for e in it:
                if e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_mtime < cutoff:
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(e.path)


# --- signature counters --------------------------------------------------------------------------------------------

def get_counter(ctx, ref):
    """The last signature counter seen for a key, or None before its first use."""
    entry = (_read(ctx, "counters.json") or {}).get(_ref(ref))
    if entry is None:
        return None
    n = entry.get("count") if isinstance(entry, dict) else None
    if type(n) is not int:
        raise Broken("counters.json: bad entry")
    return n


def set_counter(ctx, ref, n, bs):
    if type(n) is not int or not 0 <= n <= 0xFFFFFFFF or type(bs) is not bool:
        raise ValueError("a counter is 0..2**32-1 and bs a bool")
    data = _read(ctx, "counters.json") or {}
    data[_ref(ref)] = {"bs": bs, "count": n, "t": ctx.now()}
    _save(ctx, "counters.json", data)


# --- the pause -----------------------------------------------------------------------------------------------------

def pause_until(ctx):
    """None (not paused), the end time of a timed pause, or "owner" for the owner's pause, which has no end."""
    data = _read(ctx, "pause.json")
    if data is None:
        return None
    if "until" not in data:
        raise Broken("pause.json: no until")
    if data["until"] is None:
        return "owner"
    until = _number(data["until"], "pause.json")
    return until if until > ctx.now() else None


def set_pause(ctx, until, why):
    """Pause until a time, or with until=None until the owner resumes. Never shortens or ends a pause: only
    clear_pause (the owner's resume) does. True when the pause changed."""
    if until is not None and (type(until) not in (int, float) or not math.isfinite(until)):
        raise ValueError("until is a time or None")
    current = pause_until(ctx)
    if current == "owner" or (current is not None and until is not None and current >= until):
        return False
    _save(ctx, "pause.json", {"since": ctx.now(), "until": until, "why": str(why)})
    return True


def clear_pause(ctx):
    with contextlib.suppress(FileNotFoundError):
        os.unlink(ctx.lib / "pause.json")
        _fsync_dir(ctx.lib)


# --- refusal counts ------------------------------------------------------------------------------------------------

def count_refusal(ctx, cls):
    """Note one refusal of a class; return how many of that class fell within the last hour, this one included."""
    if not isinstance(cls, str) or not canon.ID_RE.fullmatch(cls):
        raise ValueError("not a refusal class")
    now = ctx.now()
    data = _read(ctx, "refusals.json") or {}
    kept = {}
    for c, ts in data.items():
        recent = [t for t in _times(ts, "refusals.json") if now - t < HOUR_S]
        if recent:
            kept[c] = recent
    kept[cls] = (kept.get(cls, []) + [now])[-MAX_KEPT:]
    _save(ctx, "refusals.json", kept)
    return len(kept[cls])


# --- attention windows (D8) ----------------------------------------------------------------------------------------

def attention_slot(ctx, kind):
    """(write_now, folded). For file-rules and watcher-failed the first in an hour writes its line and carries the count
    of those folded since the last line; later ones in that hour write none and add 1. Every other kind always writes."""
    if kind not in WINDOWED:
        return True, 0
    now = ctx.now()
    data = _read(ctx, "attention.json") or {}
    entry = data.get(kind)
    if entry is not None:
        if not isinstance(entry, dict) or type(entry.get("folded")) is not int or entry["folded"] < 0:
            raise Broken("attention.json: bad entry")
        last = _number(entry.get("last"), "attention.json")
    if entry is None or now - last >= HOUR_S:
        folded = 0 if entry is None else entry["folded"]
        data[kind] = {"folded": 0, "last": now}
        _save(ctx, "attention.json", data)
        return True, folded
    data[kind] = {"folded": entry["folded"] + 1, "last": last}
    _save(ctx, "attention.json", data)
    return False, entry["folded"] + 1


# --- per-action limits ---------------------------------------------------------------------------------------------

def _runs(ctx, action_id):
    entry = (_read(ctx, "runs.json") or {}).get(_action_id(action_id))
    if entry is None:
        return [], None
    if not isinstance(entry, dict):
        raise Broken("runs.json: bad entry")
    return _times(entry.get("day"), "runs.json"), _number(entry.get("last"), "runs.json")


def limit_reason(ctx, entry):
    """None when the action may run now, else "max-per-day" or "min-gap" (entry: id, max_per_day, min_gap_s)."""
    now = ctx.now()
    day, last = _runs(ctx, entry.id)
    if len([t for t in day if now - t < DAY_S]) >= entry.max_per_day:
        return "max-per-day"
    if last is not None and now - last < entry.min_gap_s:
        return "min-gap"
    return None


def note_run(ctx, action_id):
    """Count one run of an action now (the last time is kept apart, so a gap longer than a day still holds)."""
    now = ctx.now()
    data = _read(ctx, "runs.json") or {}
    day, _ = _runs(ctx, action_id)
    data[action_id] = {"day": [t for t in day if now - t < DAY_S] + [now], "last": now}
    _save(ctx, "runs.json", data)


# --- the lock ------------------------------------------------------------------------------------------------------

def _flock(ctx, name, why, busy=Busy):
    """Open lib/<name> and take its flock, waiting at most ctx.lock_wait_s, then raise busy(why) (Busy, or NonceBusy for
    the nonce lock); returns the fd, whose close releases the lock."""
    import fcntl
    fd = os.open(ctx.lib / name, os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + ctx.lock_wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise busy(why) from None
                time.sleep(0.05)
    except BaseException:
        os.close(fd)
        raise


@contextlib.contextmanager
def lock(ctx):
    """Hold lib/lock (flock) for one start or one command; wait at most ctx.lock_wait_s, then raise Busy."""
    fd = _flock(ctx, "lock", "another start holds the lock")
    try:
        yield
    finally:
        os.close(fd)   # closing the handle releases the lock


@contextlib.contextmanager
def _nonce_lock(ctx):
    """Hold lib/nonce-lock (flock) for one spend or one prune: a few file operations, never a whole start, so the
    runner's spend for a record refused busy at lib/lock (Z2b) takes it too. Taken after lib/lock, never before it.
    Raises NonceBusy, never a plain Busy, so the runner can tell it from lib/lock's. Windows (tests only) has no flock:
    there it holds nothing."""
    if os.name != "posix":
        yield
        return
    fd = _flock(ctx, NONCE_LOCK, "another call holds the nonce lock", NonceBusy)
    try:
        yield
    finally:
        os.close(fd)
