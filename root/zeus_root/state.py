"""Root's own state under ctx.lib (/var/lib/zeus-root): spent nonces, signature counters, the pause, refusal counts,
attention windows (D8), per-action limits, and the lock every start takes.

Callers hold lock(ctx) around every read-modify-write here (the runner for a whole start, the command line for one
change). Each file is written to a temporary name in its own folder, flushed and renamed into place, mode 600. A state
file that doesn't read back raises Broken, and the caller stops instead of guessing (fail closed). Times come from
ctx.now(); only the lock's wait uses the real monotonic clock.
"""
import contextlib
import hashlib
import math
import os
import tempfile
import time

from . import canon

HOUR_S = 3600
DAY_S = 86400
NONCE_KEEP_LIMIT = 2 ** 32             # no nonce file time past 2106-02-07 (a record expiring later is never fresh before)
WINDOWED = ("file-rules", "watcher-failed")   # D8: one line an hour; later ones are folded into the next line
MAX_KEPT = 100                                 # refusal times kept per class: enough for any "3 in an hour"
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class Broken(Exception):
    """A state file root wrote doesn't read back as expected: stop and say so, never guess."""


class Busy(Exception):
    """Another start held the lock for longer than ctx.lock_wait_s."""


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


def spend_nonce(ctx, nonce, exp=None):
    """Mark a nonce used: True the first time, False until it is pruned. exp is the signed expiry of the nonce's record
    (unix seconds), or None (anything but an int counts as None) when there is none or it was not checked: webauthn
    passes it only once the signature checked out. The nonce is kept until a day after the later of the moment it was
    spent and exp, so a genuine record refused because its exp was too far ahead cannot run once that exp comes near:
    keeping it one day only let a lying gate hand the same signed record in again a day later (0.8.1 re-check,
    finding 2)."""
    path = _nonce_path(ctx, nonce)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    except FileExistsError:
        return False
    t = ctx.now()
    if type(exp) is int and exp > t:
        t = max(t, min(exp, NONCE_KEEP_LIMIT))
    # the file's time is when it was spent by root's clock, or its record's exp if later. Set on the open file before
    # its fsync, so a crash cannot leave the file with only its creation time (kept a day instead of until exp)
    on_fd = os.utime in os.supports_fd
    try:
        if on_fd:
            os.utime(fd, (t, t))
        os.fsync(fd)
    finally:
        os.close(fd)
    if not on_fd:
        os.utime(path, (t, t))     # Windows (tests only): no utime on a file descriptor
    _fsync_dir(path.parent)
    return True


def prune_nonces(ctx):
    """Forget a nonce a day after its file's time (when it was spent, or its record's signed exp if later). By then the
    record it was spent for expired a day ago, or (exp None) was refused before its signature checked out a day ago,
    which outlasts any exp root accepts (180 s). A record with an exp more than a day ahead that root only ever saw
    spoiled is the key page's to stop (contract §8b)."""
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

@contextlib.contextmanager
def lock(ctx):
    """Hold lib/lock (flock) for one start or one command; wait at most ctx.lock_wait_s, then raise Busy."""
    import fcntl
    fd = os.open(ctx.lib / "lock", os.O_RDWR | os.O_CREAT | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + ctx.lock_wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Busy("another start holds the lock") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)   # closing the handle releases the lock
