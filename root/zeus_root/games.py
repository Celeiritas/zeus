"""Is a game running? (design §16.4 step 6 and step 8, private design 5.5 step 6 and 5.6.)

Root refuses an entry with `game_check` while a game runs, and delays a put-back by at most `reach_game_max_s`. A name
from config.json's `games` matches as a case-insensitive substring of a process's command line, as the old scripts'
`pgrep -f -i` did. The log names the process that matched (pid, user, name, program path), so a fake or forgotten
"game" shows; a program path in someone's home folder is logged from `~` on, because the log is readable by everyone
(Steam runs from the home folder: review S5). Everything is read from ctx.proc (/proc), so tests use a fake folder.
"""
import os
import re
from typing import NamedTuple, Optional

CMDLINE_MAX = 65536
_HOME_RE = re.compile(r"/(?:var/)?home/[^/]+(?=/|$)")


class Proc(NamedTuple):
    pid: int
    user: str
    comm: str
    exe: str


UNREADABLE = Proc(0, "", "", "the process list could not be read")


def _read(path, limit=CMDLINE_MAX):
    try:
        with open(path, "rb") as f:
            return f.read(limit)
    except OSError:
        return None


def _user(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def running(ctx, names) -> Optional[Proc]:
    """The first process (lowest pid) whose command line holds one of names, or None. Root's own pid is skipped, and
    so is a process without a command line (a kernel thread) or one that ended while it was read. A process list that
    cannot be read at all answers UNREADABLE, so the caller refuses (fail closed: no game check is no proof)."""
    wanted = [n.lower() for n in names if n]
    if not wanted:
        return None
    me = os.getpid()
    try:
        with os.scandir(ctx.proc) as it:
            pids = sorted(int(e.name) for e in it if e.name.isdigit())
    except OSError:
        return UNREADABLE
    for pid in pids:
        if pid == me:
            continue
        folder = ctx.proc / str(pid)
        raw = _read(folder / "cmdline")
        if not raw:
            continue
        line = raw.replace(b"\0", b" ").decode("utf-8", "replace").lower()
        if not any(n in line for n in wanted):
            continue
        try:
            uid = os.stat(folder).st_uid
        except OSError:
            continue
        comm = (_read(folder / "comm", 256) or b"").decode("utf-8", "replace").strip()
        try:
            exe = os.readlink(folder / "exe")
        except OSError:
            exe = ""
        return Proc(pid=pid, user=_user(uid), comm=comm, exe=home_as_tilde(exe))
    return None


def home_as_tilde(path):
    """/home/<name>/... and /var/home/<name>/... become ~/... (the path's start only)."""
    m = _HOME_RE.match(path)
    return "~" + path[m.end():] if m else path
