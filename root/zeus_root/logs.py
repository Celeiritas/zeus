"""The run log and its hash chain (contract §8e), the result file (§8d, D5) and the attention lines (§8f, D7, D8).

Every file here is readable by every user on the server (mode 644), so nothing written here may hold a key id, a user
handle, a public key or a user id: assert_no_ids runs before every write, at any depth. Callers hold state.lock(ctx),
except the runner's start that found it busy (Z2b); every write here also holds state.write_lock(ctx) for itself, so the
chain, a result and the attention windows never interleave between the two.
"""
import hashlib
import os
import re

from . import __version__, canon, state
from .ctx import iso

FORBIDDEN_KEYS = frozenset({"credentialId", "userHandle", "publicKey", "userId"})
RESULT_FIELDS = ("action", "state", "sentence", "started", "deadline", "remind_at", "undo_ran", "undo_ok", "class")
RESULT_KEYS = ("run",) + RESULT_FIELDS + ("updated", "zeus_root")
STATES = ("running", "waiting", "done", "stopped", "failed", "undone", "superseded", "refused")
CLASSES = ("counted", "not-counted", "garbage", "other")
# D7: the attention kinds in the order Elaia ranks them (the first buzzes loudest).
KINDS = ("copied-key", "pause", "refused-counted", "flood", "serve-changed", "list-changed", "undo-failed", "not-back",
         "put-back", "file-rules", "watcher-failed")
RUN_RE = re.compile(r"r[0-9a-f]{20}")
RUN_FILE_RE = re.compile(r"runs-[0-9]{4}-[0-9]{2}\.jsonl")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_BINARY = getattr(os, "O_BINARY", 0)   # Windows only: without it os.write turns \n into \r\n


def assert_no_ids(obj):
    """Raise ValueError when a key id, user handle, public key or user id appears as a key anywhere in obj."""
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            bad = FORBIDDEN_KEYS.intersection(x)
            if bad:
                raise ValueError("never written to a readable file: " + ", ".join(sorted(bad)))
            stack.extend(x.values())
        elif isinstance(x, (list, tuple)):
            stack.extend(x)


def _append(path, data):
    """Append bytes in one go, mode 644. If a crash left the file without its last newline, start on a fresh line."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | _NOFOLLOW | _CLOEXEC | _BINARY, 0o644)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o644)
        size = os.fstat(fd).st_size
        if size:
            os.lseek(fd, size - 1, os.SEEK_SET)
            if os.read(fd, 1) != b"\n":
                data = b"\n" + data
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _run_files(ctx):
    return sorted((p for p in ctx.log.iterdir() if RUN_FILE_RE.fullmatch(p.name)), key=lambda p: p.name)


def _last_line(path):
    data = path.read_bytes()
    if not data:
        return None
    return (data[:-1] if data.endswith(b"\n") else data).rsplit(b"\n", 1)[-1]


def append_run(ctx, line):
    """Append one line to log/runs-YYYY-MM.jsonl: adds t (if absent), zeus_root, and prev = SHA-256 hex of the exact
    bytes of the line before (the last line of an earlier file for a month's first line; null for the very first)."""
    if not isinstance(line, dict):
        raise ValueError("a log line is an object")
    assert_no_ids(line)
    with state.write_lock(ctx):
        _append_run(ctx, line)


def _append_run(ctx, line):
    now = ctx.now()
    line = dict(line)
    line.setdefault("t", iso(ctx, now))
    line["zeus_root"] = __version__
    files = _run_files(ctx)
    path = ctx.log / ("runs-" + iso(ctx, now)[:7] + ".jsonl")
    if files and files[-1].name > path.name:
        path = files[-1]   # the clock went back: stay in the newest file, so the chain stays one sequence
    line["prev"] = None
    for p in ([path] if path.exists() else []) + [f for f in reversed(files) if f.name < path.name]:
        last = _last_line(p)
        if last is not None:
            line["prev"] = hashlib.sha256(last).hexdigest()
            break
    _append(path, canon.canonical(line) + b"\n")


def iter_run_lines(ctx):
    """Every line of every runs-YYYY-MM.jsonl in order, as (path, line number, the object or None when the line is
    not a JSON object, the exact bytes)."""
    for path in _run_files(ctx):
        parts = path.read_bytes().split(b"\n")
        if parts[-1] == b"":
            parts.pop()
        for n, raw in enumerate(parts, 1):
            try:
                obj = canon.strict_loads(raw)
            except canon.Malformed:
                obj = None
            yield path, n, (obj if isinstance(obj, dict) else None), raw


def chain_problems(ctx):
    """One sentence per line that doesn't parse or whose prev doesn't match the line before; [] when the chain holds.
    (A removed last line leaves no trace in the chain itself.)"""
    problems, prev = [], None
    for path, n, obj, raw in iter_run_lines(ctx):
        where = "%s:%d" % (path.name, n)
        if obj is None:
            problems.append(where + ": not a JSON object")
        elif "prev" not in obj or obj["prev"] != prev:
            problems.append(where + ": prev does not match the line before")
        prev = hashlib.sha256(raw).hexdigest()
    return problems


def write_result(ctx, run, **fields):
    """Write or move on run/results/<run>.json (D5: always every key, null when unused; mode 644). A refusal never
    replaces an existing result (Review Focus 1: a replay has the same run id). True when written."""
    if not isinstance(run, str) or not RUN_RE.fullmatch(run):
        raise ValueError("not a run id")
    unknown = set(fields) - set(RESULT_FIELDS)
    if unknown:
        raise ValueError("not a result field: " + ", ".join(sorted(unknown)))
    if fields.get("state") not in STATES:
        raise ValueError("a result needs a state from the list")
    if fields.get("class") is not None and fields["class"] not in CLASSES:
        raise ValueError("not a refusal class")
    assert_no_ids(fields)
    with state.write_lock(ctx):
        return _write_result(ctx, run, fields)


def read_result(ctx, run):
    """The result of a run as written, or None when there is none (a run id only; raises state.Broken when it does
    not read back)."""
    if not isinstance(run, str) or not RUN_RE.fullmatch(run):
        raise ValueError("not a run id")
    try:
        raw = (ctx.run / "results" / (run + ".json")).read_bytes()
    except FileNotFoundError:
        return None
    try:
        obj = canon.strict_loads(raw)
    except canon.Malformed:
        obj = None
    if not isinstance(obj, dict) or set(obj) != set(RESULT_KEYS):
        raise state.Broken(run + ".json does not read back")
    return obj


def _write_result(ctx, run, fields):
    path = ctx.run / "results" / (run + ".json")
    old = None
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        pass
    else:
        if fields["state"] == "refused":
            return False
        try:
            old = canon.strict_loads(raw)
        except canon.Malformed:
            old = None
        if not isinstance(old, dict) or set(old) != set(RESULT_KEYS):
            raise state.Broken(path.name + " does not read back")
    now = iso(ctx, ctx.now())
    result = dict.fromkeys(RESULT_KEYS) if old is None else old
    result.update(fields)
    result.update(run=run, updated=now, zeus_root=__version__)
    if result["started"] is None:
        result["started"] = now
    state.write_file(path, canon.canonical(result) + b"\n", 0o644)
    return True


def attention(ctx, kind, sentence, run):
    """Write one attention line {t, run, kind, sentence, folded} unless D8 folds it into the next one. True if written."""
    if kind not in KINDS:
        raise ValueError("not an attention kind")
    if run is not None and (not isinstance(run, str) or not RUN_RE.fullmatch(run)):
        raise ValueError("not a run id")
    if not isinstance(sentence, str) or not sentence.strip():
        raise ValueError("an attention line needs its sentence")
    with state.write_lock(ctx):
        write, folded = state.attention_slot(ctx, kind)
        if not write:
            return False
        line = {"folded": folded, "kind": kind, "run": run, "sentence": sentence, "t": iso(ctx, ctx.now())}
        _append(ctx.log / "attention.jsonl", canon.canonical(line) + b"\n")
    return True
