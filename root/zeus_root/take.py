"""The hostile take (design 5.5 steps 1-2) and the quarantine (contract §8e, D6).

The inbox belongs to the gate account, so everything in it is treated as hostile. take() lists it without following
anything and renames every entry of every kind (file, folder, FIFO, link, socket) into run/work/ under a name root
chooses, before looking at any of them. Once there (root 700) the gate can't reach it any more. open_entry() then
reads at most 16 KB + 1 byte from a handle, never from the name again, and only from a regular file owned by the gate
with one link. Anything else is quarantined: every entry leaves the inbox, so the path unit can never loop on it.
"""
import hashlib
import os
import re
import stat
import time
from typing import List, NamedTuple, Optional

from . import logs

TAKE_LIMIT = 50                 # entries taken per start; more waiting is a flood (the runner pauses)
FLOOD_QUARANTINE = 200          # more than this in quarantine is a flood too
ENTRY_LIMIT = 16384             # bytes; 16385 are read so a bigger file shows
REASONS = {"malformed": "garbage", "leftover": "other"}   # quarantine reasons besides file-rules, and their class
_OPEN = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)

_after_listing = None           # tests only: called with the listed names between the listing and the renames


class Taken(NamedTuple):
    paths: List                 # the entries now in run/work/, in the order taken
    left: int                   # entries still waiting beyond the limit


class Bad(NamedTuple):
    type: str                   # file, folder, link, fifo, socket, device, other
    size: int
    uid: int
    sha256_16k: Optional[str]   # of the first 16 KB, for regular files only (nothing else is ever opened)
    why: str


def _kind(mode):
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "folder"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISFIFO(mode):
        return "fifo"
    if stat.S_ISSOCK(mode):
        return "socket"
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return "device"
    return "other"


def take(ctx, src, prefix, limit=TAKE_LIMIT):
    """Rename up to `limit` entries of src, of every kind, into run/work/<prefix>-<ns>-<n>-<hex>. A name that vanished
    in between is skipped. Nothing is opened or followed here."""
    if not re.fullmatch(r"[a-z]{1,16}", prefix):
        raise ValueError("a take prefix is a short lowercase word")
    with os.scandir(src) as it:
        names = sorted(e.name for e in it)
    if _after_listing is not None:
        _after_listing(names)
    work, ns, paths = ctx.run / "work", time.time_ns(), []
    for i, name in enumerate(names[:limit]):
        dest = work / ("%s-%020d-%03d-%s" % (prefix, ns, i, os.urandom(4).hex()))
        try:
            os.rename(os.path.join(src, name), dest)
        except FileNotFoundError:
            continue
        paths.append(dest)
    return Taken(paths, max(0, len(names) - limit))


def _read(fd, n):
    chunks, got = [], 0
    while got < n:
        b = os.read(fd, n - got)
        if not b:
            break
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


def open_entry(ctx, path):
    """The entry's bytes, or Bad. Only a regular file is ever opened (O_NOFOLLOW, O_NONBLOCK); fstat on the handle must
    show a regular file owned by the gate with one link and at most 16 KB, and the bytes come from that handle."""
    st = os.lstat(path)
    kind = _kind(st.st_mode)
    if kind != "file":
        return Bad(kind, st.st_size, st.st_uid, None, "a link" if kind == "link" else "not a regular file")
    try:
        fd = os.open(path, _OPEN)
    except OSError:
        return Bad("other", st.st_size, st.st_uid, None, "would not open")
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return Bad(_kind(st.st_mode), st.st_size, st.st_uid, None, "not a regular file")
        data = _read(fd, ENTRY_LIMIT + 1)
    finally:
        os.close(fd)
    why = None
    if st.st_uid != ctx.gate_uid:
        why = "wrong owner"
    elif st.st_nlink != 1:
        why = "a hard link"
    elif st.st_size > ENTRY_LIMIT or len(data) > ENTRY_LIMIT:
        why = "too big"
    if why:
        return Bad("file", st.st_size, st.st_uid, hashlib.sha256(data[:ENTRY_LIMIT]).hexdigest(), why)
    return data


def _describe(path):
    """type, size, owner and the checksum of the first 16 KB of an entry that read fine (for its log line)."""
    st = os.lstat(path)
    sha = None
    if stat.S_ISREG(st.st_mode):
        fd = os.open(path, _OPEN)
        try:
            sha = hashlib.sha256(_read(fd, ENTRY_LIMIT)).hexdigest()
        finally:
            os.close(fd)
    return {"type": _kind(st.st_mode), "size": st.st_size, "uid": st.st_uid, "sha256_16k": sha}


def quarantine(ctx, path, bad, reason="malformed"):
    """Move a taken entry into run/quarantine/ and write its log line (never its content). bad (from open_entry) means
    it broke the file rules: class other, reason file-rules, and an attention line (at most one an hour, D8).
    Otherwise reason is "malformed" (class garbage: no attention line, contract 8f) or "leftover" (class other)."""
    if bad is not None:
        entry = {"type": bad.type, "size": bad.size, "uid": bad.uid, "sha256_16k": bad.sha256_16k}
        cls, reason = "other", "file-rules"
    else:
        if reason not in REASONS:
            raise ValueError("not a quarantine reason")
        entry, cls = _describe(path), REASONS[reason]
    os.rename(path, ctx.run / "quarantine" / path.name)
    line = {"run": None, "outcome": "refused", "class": cls, "reason": reason, "entry": entry}
    if bad is not None:
        line["why"] = bad.why
    logs.append_run(ctx, line)
    if bad is not None:
        logs.attention(ctx, "file-rules", "Zeus refused an inbox entry that broke the file rules (%s); nothing ran"
                       % bad.why, None)


def quarantine_count(ctx):
    with os.scandir(ctx.run / "quarantine") as it:
        return sum(1 for _ in it)


def leftovers(ctx):
    """Everything already in run/work/ when a start begins: left by a start that crashed. Never re-run."""
    with os.scandir(ctx.run / "work") as it:
        return sorted(ctx.run / "work" / e.name for e in it)
