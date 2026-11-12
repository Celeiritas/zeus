"""The one object every zeus-root part takes: paths, account ids, the clock, the time zone, outside commands, questions.

Production builds it with `Ctx.system()` from constants only: no environment variable, option or user-writable file
changes a path, an id, a command or a limit (design §16). Tests build their own on temporary folders
(tests/root_support.py: make_ctx) and shorten the limits through it.
"""
import io
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from pathlib import Path
from typing import Callable, NamedTuple, Optional

GATE_ACCOUNT = "zeus-root-gate"
COMMAND_PATH = "/usr/sbin:/usr/bin"
LOCALTIME = "/etc/localtime"
ZONE_FILE_MAX = 64 * 1024              # a real zone file is a few KB


class CmdResult(NamedTuple):
    code: int
    out: str
    err: str


class NoTerminal(Exception):
    """ask() found no terminal to put the question on (a timer or a pipe, not a person at a keyboard)."""


@dataclass
class Ctx:
    etc: Path              # /etc/zeus-root: config, actions, keys, owners
    run: Path              # /run/zeus-root: inbox, enrol, work, quarantine, results
    lib: Path              # /var/lib/zeus-root: nonces, counters, reach, attention counts
    log: Path              # /var/log/zeus-root: run log, attention lines
    libexec: Path          # /usr/local/libexec/zeus-root: the listed scripts
    proc: Path             # /proc
    root_uid: int
    gate_uid: int
    gate_gid: int
    now: Callable[[], float]
    tz: Optional[tzinfo]   # Ctx.system(): the zone of /etc/localtime (server_zone); None = Python's local zone
    cmd: Callable[[list, float], CmdResult]
    ask: Callable[[str], str]
    script_limit_s: float = 120
    undo_limit_s: float = 120
    lock_wait_s: float = 30

    @classmethod
    def system(cls):
        """The real paths and accounts. Refuses when the gate account is missing or is root."""
        import grp
        import pwd
        try:
            gate_uid = pwd.getpwnam(GATE_ACCOUNT).pw_uid
            gate_gid = grp.getgrnam(GATE_ACCOUNT).gr_gid
        except KeyError:
            raise RuntimeError("no " + GATE_ACCOUNT + " account: zeus-root is not installed") from None
        if gate_uid == 0 or gate_gid == 0:
            raise RuntimeError(GATE_ACCOUNT + " must not be root")
        return cls(etc=Path("/etc/zeus-root"), run=Path("/run/zeus-root"), lib=Path("/var/lib/zeus-root"),
                   log=Path("/var/log/zeus-root"), libexec=Path("/usr/local/libexec/zeus-root"), proc=Path("/proc"),
                   root_uid=0, gate_uid=gate_uid, gate_gid=gate_gid, now=time.time, tz=server_zone(),
                   cmd=run_command, ask=ask_terminal)


def server_zone(path=LOCALTIME):
    """The server's zone read from /etc/localtime itself (root-owned), never from TZ: sudo passes TZ through, so a
    normal user could otherwise shift every time zeus-root writes (Z2a hostile review, finding 7). Each moment keeps
    its own offset, so summer time comes out right.

    The file is read whole (at most ZONE_FILE_MAX bytes) and parsed from memory by a reader that refuses to read past
    its end: CPython's zoneinfo reads a version 2 footer one byte at a time until a newline and loops for ever when the
    file ends first, and raises struct.error or AssertionError on some cut files (re-check R2). A missing, unreadable,
    oversized or damaged file, or any error while parsing it, gives UTC: a wrong offset in a log line is better than a
    runner that never starts."""
    try:
        with open(path, "rb") as f:
            data = f.read(ZONE_FILE_MAX + 1)
    except OSError:
        return timezone.utc
    if len(data) > ZONE_FILE_MAX:
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo.from_file(_NoReadPastEnd(data))
    except Exception:
        return timezone.utc


class _NoReadPastEnd(io.BytesIO):
    """A zone file in memory whose read(n) raises EOFError when fewer than n bytes are left (n < 0 reads the rest)."""

    def read(self, n=-1):
        b = super().read(n)
        if n is not None and n >= 0 and len(b) < n:
            raise EOFError("the zone file ends early")
        return b


def _text(b):
    return b.decode("utf-8", "replace") if b else ""


def run_command(argv, timeout):
    """One outside command (systemctl, tailscale): absolute path, fixed argv, PATH only, cwd /, no stdin, no shell.
    A timeout answers 124 and a program that can't start 127, as the shell's `timeout` and `command` do."""
    if not argv or not all(isinstance(a, str) for a in argv) or not argv[0].startswith("/"):
        raise ValueError("a command is a list of strings starting with an absolute path")
    try:
        p = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, cwd="/",
                           env={"PATH": COMMAND_PATH}, close_fds=True)
    except subprocess.TimeoutExpired as e:
        return CmdResult(124, _text(e.stdout), "timed out after %g s" % timeout)
    except OSError as e:
        return CmdResult(127, "", "could not start: " + (e.strerror or type(e).__name__))
    return CmdResult(p.returncode, _text(p.stdout), _text(p.stderr))


def ask_terminal(prompt):
    """Put one question on the controlling terminal and read one line back. Never stdin, which a caller can fill
    from a pipe: the typed questions of `enroll` must come from a person. No terminal raises NoTerminal."""
    try:
        with open("/dev/tty", "w", encoding="utf-8") as out, \
                open("/dev/tty", encoding="utf-8", errors="replace") as inp:
            out.write(prompt)
            out.flush()
            line = inp.readline()
    except OSError:
        raise NoTerminal("no terminal: run this in a terminal") from None
    if not line:
        raise NoTerminal("the terminal closed")
    return line.rstrip("\r\n")


def iso(ctx, t):
    """ISO 8601 to the second with the offset of ctx.tz at that moment, e.g. 2026-11-11T19:05:00-05:00."""
    d = datetime.fromtimestamp(t, ctx.tz) if ctx.tz is not None else datetime.fromtimestamp(t).astimezone()
    return d.isoformat(timespec="seconds")
