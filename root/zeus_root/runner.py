"""The runner: one start of zeus-root-run.service (design §16.4 steps 6-9, private design 5.5, plan D10 and Task 7).

A start, under lib/lock:
1. root's own files first (the file rules of design §16.3, then config.json, actions.json and keys.json): when one is
   wrong, nothing runs in this start, not even a put-back, and every record taken is refused `root-files`;
2. reach deadlines that passed without a tick (a lost timer) are put back (reach.due);
3. leftovers in work/ (a crashed start) go to quarantine, their nonce spent if readable, never re-run; each takes a
   slot of the hourly cap first (past it, it waits in work/ for a later hour), and a busy start's busy-* entries are
   left alone while it holds lib/busy-lock;
4. spent nonces older than their keep are pruned;
5. at most 50 inbox entries are taken (take.take); more than 50 waiting or more than 200 in quarantine is a flood:
   root changes pause for an hour, and one `flood` attention line is written unless a flood was already seen within
   the hour before (state.flood_new), whatever pause is on: one line for a flood that lasts (D9, review N2); the
   flood is noted (state.note_flood) only after its line is written, so a line that failed comes at the next start;
6. each entry: the hourly cap (state.open_slot: every start together, the busy one included, opens at most 50 inbox
   entries an hour; past it an entry goes to quarantine unopened, and the start writes one `open-cap` line for all of
   them: review M1), the file rules of the entry, the strict parse (both quarantine, spending nothing and answering
   nothing), then a check-in (reach.check_in) or a run or check-key record: webauthn.verify (which spends
   the nonce whatever it answers), then the pause, Serve's mapping and the operator, the limits, at most one script per
   start, a running game, the script's hash, and the run with hold-or-undo-once.

A start that finds lib/lock held for longer than ctx.lock_wait_s (a put-back or a command holds it) takes nothing when
it sees a flood (the start that next holds lib/lock applies the flood rule; until then the path unit starts it again,
each try waiting for the lock), and otherwise answers every entry it takes `busy`, within the same hourly cap. Before
that it checks each record up to and including its signature (plan v6, Dax 2026-11-12) and
spends the nonce with the signed exp when the signature checked out, else with none: a record root refused busy can
never run later when sent again as it was. It writes its lines under state.write_lock only, and touches no state but
the nonces, the hourly cap and quarantine. zeus-root-run.service is a oneshot, so two run starts never overlap; a
busy start overlaps only reach-tick, boot or a command. It holds lib/busy-lock while it takes and answers its entries
(waiting at most ctx.lock_wait_s for it, else taking nothing), and leftovers leave busy-* entries in work/ while that
lock is held, so a boot that takes lib/lock meanwhile never quarantines an entry the busy start still answers; an
entry gone meanwhile is skipped (re-check notes, item 4). A busy start that dies releases the lock, and what it left
in work/ is a leftover for the next start.

Every refusal is one run-log line with its reason and class (contract §8e) and, when its record names a run, a
`refused` result written after its nonce was spent. A spend that fails (state.NonceBusy, state.Broken, any OSError)
writes no result, is not answered again in this start, and leaves the entry in work/: the next start quarantines it
as a leftover and spends its nonce then. Which refusals write an attention line is contract §8f (`_attend`).
"""
import hashlib
import hmac
import os
import signal
import subprocess
import time
from typing import NamedTuple, Optional

from . import canon, config, games, logs, reach, serve, state, take, webauthn
from .canon import Malformed
from .ctx import COMMAND_PATH, iso
from .webauthn import Refusal

HOUR_S = 3600
PAUSE_COUNTED = 3                   # counted refusals within an hour that pause root changes for an hour (D9)
OPEN_PER_HOUR = 50                  # inbox entries every start together opens an hour; past it, quarantined unread
STAGE_LINE = b"ZEUS_STAGE=change"
REACH_SCHEMA = {"type": str, "run": str, "seen": dict}
ASSERTION_FIELDS = ("clientDataJSON", "authenticatorData", "signature")
_OPEN = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_LOG_OPEN = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

# The runner's own reasons (contract §8e): reason -> (class, attention kind or None, root's sentence for the key page).
# {title} is the entry's title. Serve's reasons are serve.REASONS (class other, attention serve-changed).
RUNNER_REFUSALS = {
    "busy": ("other", None, "Another root change was running, so this one did not start. Nothing ran; try again."),
    "paused": ("other", None, "Root changes are paused for now. Nothing ran."),
    "rate-limit": ("other", None, "{title} ran too recently or too often today. Nothing ran."),
    "game": ("other", None, "A game is running, so {title} did not start. Nothing ran."),
    "script-changed": ("other", "list-changed", "The script of {title} changed since the list was made. Nothing ran."),
    "root-files": ("other", "file-rules", "Root's own files break the ownership or mode rules. Nothing ran."),
    "not-listed": ("other", None, "This action is not on the list. Nothing ran."),
    "malformed": ("garbage", None, "The request was not in the expected shape. Nothing ran."),
}
for _reason, _sentence in serve.REASONS.items():
    RUNNER_REFUSALS[_reason] = ("other", "serve-changed", _sentence)

SENTENCES = {
    "stopped": "{title} stopped before it changed anything.",
    "failed-undone": "{title} failed after it changed something, and it was put back.",
    "failed-undo-failed": "{title} failed after it changed something, and putting it back failed too.",
    "not-back": "{title}: the service did not come back up",
    "undo-failed": "{title} failed, and its undo failed too",
    "key-checked": "This key checked out. Nothing ran.",
    "crashed": "Root stopped while {title} ran, so it may be half done; nothing was put back.",
    "pause": "Zeus refused 3 tries; root changes are paused for an hour",
}


class ScriptOutcome(NamedTuple):
    exit: int               # the script's exit code; a negative number is the signal that ended it; 127: did not start
    staged: bool            # it printed ZEUS_STAGE=change at the end of a line
    secs: float
    timed_out: bool         # its limit ran out and its whole process group was killed
    started: bool           # False when it could not start at all (exit 127 then; a script's own 127 says True)


class Loaded(NamedTuple):
    cfg: object             # config.Config
    actions: dict           # id -> config.Action
    list_sha: str
    keys: dict              # ref -> config.Key


class TreeProblem(Exception):
    """Root's own files break the file rules, or one of them does not load: nothing runs (design §16.3)."""


def load(ctx):
    """The file rules first, then config.json, actions.json and keys.json. Raises TreeProblem."""
    problems = config.file_problems(ctx)
    if problems:
        raise TreeProblem(problems[0])
    try:
        cfg = config.load_config(ctx)
        actions, list_sha = config.load_actions(ctx)
        keys = config.load_keys(ctx)
    except (config.ConfigInvalid, config.ListInvalid, config.KeysInvalid) as e:
        raise TreeProblem(str(e)) from None
    return Loaded(cfg, actions, list_sha, keys)


def refusal(reason, title=None):
    """A Refusal for one of the runner's own reasons, its sentence filled in."""
    cls, attention, sentence = RUNNER_REFUSALS[reason]
    return Refusal(reason, cls, attention, sentence.format(title=title or "This action"))


def sentence(name, title):
    return SENTENCES[name].format(title=title)


# --- the record ----------------------------------------------------------------------------------------------------

def parse_record(data):
    """The strict parse of an inbox entry (D1, D3): strict JSON, exactly the keys and types of a run or check-key
    record or of a check-in, `nonce` a canonical 32-byte base64url, `action` an id (check-key included), `list` 64
    lowercase hex, `exp` an int, the assertion fields base64url (userHandle may be null), `seen` exactly
    {login, node, node_id, ip} of strings; a check-in's `run` a run id. Anything else raises Malformed, and the entry
    is quarantined as garbage without spending anything (contract §8b)."""
    obj = canon.strict_loads(data)
    if not isinstance(obj, dict):
        raise Malformed("not an object")
    if obj.get("type") == "reach":
        canon.require(obj, REACH_SCHEMA)
        canon.require(obj["seen"], webauthn.SEEN_SCHEMA)
        if not logs.RUN_RE.fullmatch(obj["run"]):
            raise Malformed("bad run")
        return obj
    if obj.get("type") not in webauthn.RECORD_TYPES:
        raise Malformed("bad type")
    canon.require(obj, webauthn.RECORD_SCHEMA)
    canon.require(obj["seen"], webauthn.SEEN_SCHEMA)
    if not canon.valid_nonce(obj["nonce"]):
        raise Malformed("bad nonce")
    if not canon.ID_RE.fullmatch(obj["action"]):
        raise Malformed("bad action")
    if not canon.HEX64_RE.fullmatch(obj["list"]):
        raise Malformed("bad list")
    for field in ("credentialId",) + ASSERTION_FIELDS:
        canon.b64u_decode(obj[field])
    if obj["userHandle"] is not None:
        canon.b64u_decode(obj["userHandle"])
    return obj


# --- running one listed script -------------------------------------------------------------------------------------

def script_path(ctx, entry):
    return ctx.libexec / "actions" / entry.script


def script_matches(ctx, entry):
    """True when the entry's script, opened without following a link, hashes to the entry's sha256."""
    h = hashlib.sha256()
    try:
        fd = os.open(script_path(ctx, entry), _OPEN)
    except OSError:
        return False
    try:
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            h.update(b)
    except OSError:
        return False
    finally:
        os.close(fd)
    return hmac.compare_digest(h.hexdigest(), entry.sha256)


def _staged(log_path, offset):
    """Whether the log, from offset on, holds a line that ends in ZEUS_STAGE=change (a CR before the newline too). A
    line that only ends in it counts too (review N5): a script that printed part of a line just before its stage line
    still undoes a failure; reading too much as staged costs an undo, reading too little would skip one."""
    try:
        with open(log_path, "rb") as f:
            f.seek(offset)
            for line in f:
                if line.rstrip(b"\r\n").endswith(STAGE_LINE):
                    return True
    except OSError:
        return False
    return False


def run_entry(ctx, entry, run, limit_s, log_path):
    """Run one listed script once: re-hashed right before (a mismatch raises the `script-changed` Refusal), no
    arguments, working folder /, stdin from /dev/null (a read ends at once), an environment of exactly PATH and
    ZEUS_ROOT_RUN=<run>, its own process group (start_new_session), stdout and stderr appended to log_path (644). When
    limit_s runs out the whole group is killed. Returns ScriptOutcome; a script that cannot start answers exit 127."""
    if not script_matches(ctx, entry):
        raise refusal("script-changed", entry.title)
    fd = os.open(log_path, _LOG_OPEN, 0o644)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o644)
        offset = os.fstat(fd).st_size
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen([str(script_path(ctx, entry))], stdin=subprocess.DEVNULL, stdout=fd, stderr=fd,
                                    cwd="/", env={"PATH": COMMAND_PATH, "ZEUS_ROOT_RUN": run}, close_fds=True,
                                    start_new_session=True)
        except OSError:
            return ScriptOutcome(127, False, round(time.monotonic() - t0, 3), False, False)
        timed_out = False
        try:
            code = proc.wait(timeout=limit_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = proc.wait()
        secs = round(time.monotonic() - t0, 3)
    finally:
        os.close(fd)
    return ScriptOutcome(code, _staged(log_path, offset), secs, timed_out, True)


def log_path(ctx, run):
    return ctx.log / ("run-%s.log" % run)


def undo_run_id(run):
    """The run id of an undo: "u" + the run's 20 hex (the failure's undo, or a put-back's; never both for one run)."""
    return "u" + run[1:]


# --- one start -----------------------------------------------------------------------------------------------------

class _Start:
    def __init__(self, ctx, loaded, problem):
        self.ctx = ctx
        self.loaded = loaded        # Loaded, or None when problem says why not
        self.problem = problem
        self.ran = False            # a script ran in this start (a put-back's undo included)
        self.over_cap = 0           # entries set aside unread past the hourly cap


def load_or_problem(ctx):
    """(Loaded, None), or (None, why) when root's own files break the rules or do not load."""
    try:
        return load(ctx), None
    except TreeProblem as e:
        return None, str(e)


def start(ctx):
    """One start of the runner; returns the exit code (0). Raises on a state file that does not read back
    (state.Broken), on a write that fails, or when the inbox cannot be listed: the unit then fails, which Elaia's
    watch shows, and the entries already taken wait in work/ for the next start."""
    lock = state.lock(ctx)
    try:
        lock.__enter__()
    except state.Busy as e:
        if type(e) is not state.Busy:
            raise
        return _busy_start(ctx)
    try:
        return _locked_start(ctx)
    finally:
        lock.__exit__(None, None, None)


def _locked_start(ctx):
    loaded, problem = load_or_problem(ctx)
    s = _Start(ctx, loaded, problem)
    if problem is None:
        s.ran = reach.due(ctx, loaded) > 0
    else:
        logs.attention(ctx, "file-rules", "Root's own files break the ownership or mode rules (%s); nothing runs "
                       "until they are fixed" % problem, None)
    leftovers(ctx, loaded)
    state.prune_nonces(ctx)
    taken = take.take(ctx, ctx.run / "inbox", "run")
    _flood(ctx, taken)
    for path in taken.paths:
        _entry(s, path)
    _cap_line(ctx, s.over_cap)
    return 0


def _cap_line(ctx, count):
    """The one line for every entry a start set aside unopened past the hourly cap (class garbage, so Elaia's
    garbage warning sees it)."""
    if count:
        logs.append_run(ctx, {"run": None, "outcome": "refused", "class": "garbage", "reason": "open-cap",
                              "count": count})


def _flood(ctx, taken):
    """More than 50 waiting or more than 200 in quarantine: pause root changes for an hour (the owner's pause stays
    as it is), and one `flood` line unless a flood was already seen within the hour before: one line for a flood that
    lasts, whatever pause is on, the owner's included (only the cause buzzes, D9; review N2)."""
    in_quarantine = take.quarantine_count(ctx)
    if taken.left == 0 and in_quarantine <= take.FLOOD_QUARANTINE:
        return
    new = state.flood_new(ctx)
    state.set_pause(ctx, ctx.now() + HOUR_S, "flood")
    if new:
        paused = ("stay paused until you resume them" if state.pause_until(ctx) == "owner"
                  else "are paused for an hour")
        logs.attention(ctx, "flood", "Zeus's inbox was flooded (%d waiting, %d in quarantine); root changes %s"
                       % (len(taken.paths) + taken.left, in_quarantine, paused), None)
    state.note_flood(ctx)        # after the line: a line that failed to write comes at the next start (item 3)


def leftovers(ctx, loaded):
    """Every entry already in work/ when a start (or boot) begins was left by a crashed start: its nonce is spent if
    it reads as a record with a canonical nonce (exp None: its signature is not checked), a result that still says
    running becomes failed (the change may be half done; nothing is put back), and it goes to quarantine, never
    re-run. A spend that fails is logged and the entry is quarantined anyway. loaded (None when root's files are
    broken) gives the crashed sentence its title. Each leftover takes a slot of the hourly cap first, as every opened
    entry does (re-check notes, item 2); past it, it and the rest wait in work/, unread and unspent, for a start in a
    later hour. While a start that found lib/lock busy holds lib/busy-lock, its busy-* entries are its own, not
    leftovers (item 4)."""
    with state.busy_lock(ctx, wait=False) as free:
        for path in take.leftovers(ctx):
            if not free and path.name.startswith("busy-"):
                continue
            if not state.open_slot(ctx, OPEN_PER_HOUR):
                break
            _leftover(ctx, path, loaded)


def _leftover(ctx, path, loaded):
    try:
        data = take.open_entry(ctx, path)
    except FileNotFoundError:
        return
        nonce = None
        if isinstance(data, (bytes, bytearray)):
            try:
                obj = canon.strict_loads(data)
            except Malformed:
                obj = None
            if isinstance(obj, dict) and canon.valid_nonce(obj.get("nonce")):
                nonce = obj["nonce"]
    if nonce is not None:
        run = canon.run_id(nonce)
        try:
            state.spend_nonce(ctx, nonce, None)
        except (state.Busy, state.Broken, OSError) as e:
            logs.append_run(ctx, {"run": run, "outcome": "refused", "class": "other", "reason": "nonce-store",
                                  "error": type(e).__name__})
        _crashed_result(ctx, run, loaded)
    try:
        take.quarantine(ctx, path, None, "leftover")
    except FileNotFoundError:
        return


def _crashed_result(ctx, run, loaded):
    """A result that still says running becomes failed. The sentence names the action from root's own result and
    root's list, never from the leftover, whose text the gate wrote (review S6)."""
    try:
        res = logs.read_result(ctx, run)
    except state.Broken:
        return
    if res is not None and res["state"] == "running":
        entry = loaded.actions.get(res["action"]) if loaded is not None else None
        title = entry.title if entry is not None else "This action"
        logs.write_result(ctx, run, state="failed", sentence=sentence("crashed", title), undo_ran=False)


def _opened(s, path):
    """One slot of the hour's cap for this entry, or the entry goes to quarantine unopened (review M1)."""
    if state.open_slot(s.ctx, OPEN_PER_HOUR):
        return True
    take.quarantine_unopened(s.ctx, path)
    s.over_cap += 1
    return False


def _entry(s, path):
    ctx = s.ctx
    if not _opened(s, path):
        return
    data = take.open_entry(ctx, path)
    if isinstance(data, take.Bad):
        take.quarantine(ctx, path, data)
        return
    try:
        rec = parse_record(data)
    except Malformed:
        take.quarantine(ctx, path, None, "malformed", data=data)
        return
    if rec["type"] == "reach":
        _check_in(s, rec)
    elif s.problem is not None:
        if not _refuse_unverified(s, rec, refusal("root-files"), {"why": s.problem}):
            return
    elif not _record(s, rec):
        return                    # a spend failed: the entry stays in work/ for the next start
    os.unlink(path)


def _check_in(s, rec):
    if reach.check_in(s.ctx, rec, s.loaded):
        return
    logs.append_run(s.ctx, {"run": rec["run"], "outcome": "refused", "class": "garbage",
                            "reason": "check-in-refused", "seen": _seen(rec)})


def _seen(rec):
    return {k: rec["seen"][k] for k in ("login", "node", "ip")}


def _key_of(rec):
    return canon.key_ref(canon.b64u_decode(rec["credentialId"]))


def _line(s, rec, run, v=None):
    """The run-log line's common part: no key id, no user handle (contract §8e)."""
    ref = _key_of(rec)
    key = (s.loaded.keys if s.loaded else {}).get(ref)
    line = {"run": run, "action": rec["action"], "seen": _seen(rec),
            "assertion": {f: rec[f] for f in ASSERTION_FIELDS}, "list": rec["list"]}
    if key is not None and v is not None:
        line["key"] = {"ref": key.ref, "label": key.label, "device": key.node_name,
                       "synced_claimed": key.claim == "synced"}
    else:
        line["key"] = {"ref": ref}
    if v is not None:
        line["uv"] = v.auth.uv
        line["challenge"] = v.challenge.hex()
    return line


def _refuse_unverified(s, rec, r, extra):
    """Refuse a record root does not verify in this start (root's files broken): spend its nonce without an exp,
    then the line and the result. False when the spend failed (nothing written; the entry waits in work/)."""
    run = canon.run_id(rec["nonce"])
    try:
        state.spend_nonce(s.ctx, rec["nonce"], None)
    except (state.Busy, state.Broken, OSError) as e:
        _spend_failed(s, rec, run, e, None)
        return False
    _refused(s, rec, run, r, None, extra)
    return True


def _record(s, rec):
    """A run or check-key record, in D10's order after the strict parse. False when a spend failed."""
    ctx, ld = s.ctx, s.loaded
    run = canon.run_id(rec["nonce"])
    counter, last_exp = state.key_state(ctx, _key_of(rec))

    def spend(nonce, exp):
        return state.spend_nonce(ctx, nonce, exp)
    try:
        v = webauthn.verify(rec, ld.keys, ld.cfg, ld.list_sha, ctx.now(), counter, spend, last_exp)
    except Refusal as r:
        _refused(s, rec, run, r, None)
        return True
    except Malformed:
        _refused(s, rec, run, refusal("malformed"), None)
        return True
    except (state.Busy, state.Broken, OSError) as e:
        _spend_failed(s, rec, run, e, e.__context__ if isinstance(e.__context__, Refusal) else None)
        return False
    state.set_counter(ctx, v.key.ref, v.auth.count, v.auth.bs, rec["exp"])
    if rec["type"] == "check-key":
        logs.append_run(ctx, dict(_line(s, rec, run, v), outcome="key-checked"))
        logs.write_result(ctx, run, action=rec["action"], state="done", sentence=SENTENCES["key-checked"])
        return True
    entry = ld.actions.get(rec["action"])
    if entry is None:
        _refused(s, rec, run, refusal("not-listed"), v)
        return True
    r, extra = _runner_checks(s, entry)
    if r is not None:
        _refused(s, rec, run, r, v, extra)
        return True
    _run(s, rec, run, entry, v)
    return True


def _runner_checks(s, entry):
    """D10 after the assertion: the pause, Serve's mapping and the operator, the limits, one script per start, a game,
    the script's hash. (Refusal, extra log fields) or (None, None)."""
    ctx, cfg = s.ctx, s.loaded.cfg
    if state.pause_until(ctx) is not None:
        return refusal("paused", entry.title), {}
    reason = serve.problem(ctx, cfg)
    if reason is not None:
        return refusal(reason, entry.title), {}
    limit = state.limit_reason(ctx, entry)
    if limit is not None:
        return refusal("rate-limit", entry.title), {"limit": limit}
    if s.ran:
        return refusal("busy", entry.title), {"why": "one script per start"}
    if entry.game_check:
        proc = games.running(ctx, cfg.games)
        if proc is not None:
            return refusal("game", entry.title), {"process": proc._asdict()}
    if not script_matches(ctx, entry):
        return refusal("script-changed", entry.title), {}
    return None, None


def _refused(s, rec, run, r, v, extra=None):
    """One refused line, the refused result (never over an existing one), and the attention rule (_attend)."""
    line = _line(s, rec, run, v)
    line.update(extra or {})
    line.update({"outcome": "refused", "reason": r.reason, "class": r.cls})
    logs.append_run(s.ctx, line)
    logs.write_result(s.ctx, run, action=rec["action"], state="refused", sentence=r.sentence, **{"class": r.cls})
    _attend(s.ctx, r, run)


def _attend(ctx, r, run):
    """Contract §8f and D9: garbage and not-counted write no line (Elaia counts garbage from the log); a counted
    refusal while paused writes none; the one that makes the third in an hour pauses for an hour and writes `pause`
    instead of its own line; any other counted refusal writes `refused-counted`; the rest write the kind their reason
    names, paused or not."""
    if r.cls == "garbage":
        return
    if r.cls == "counted":
        paused = state.pause_until(ctx) is not None
        n = state.count_refusal(ctx, "counted")
        if paused:
            return
        if n >= PAUSE_COUNTED:
            state.set_pause(ctx, ctx.now() + HOUR_S, "%d counted refusals within an hour" % n)
            logs.attention(ctx, "pause", SENTENCES["pause"], run)
            return
        logs.attention(ctx, "refused-counted", "Zeus refused a try. " + r.sentence, run)
        return
    if r.attention is not None:
        logs.attention(ctx, r.attention, r.sentence, run)


def _spend_failed(s, rec, run, e, cause):
    """The nonce store failed (NonceBusy, Broken, an OSError): no result, nothing answered again in this start. The
    refusal verify was raising when it failed (cause, from the error's __context__) is logged with its reason and
    class and still counted, so a forgery handed in while the disk is full counts toward the pause (plan v4)."""
    line = _line(s, rec, run)
    if cause is not None:
        line.update({"outcome": "refused", "reason": cause.reason, "class": cause.cls})
    else:
        line.update({"outcome": "refused", "reason": "nonce-store", "class": "other"})
    line["nonce_store"] = type(e).__name__
    logs.append_run(s.ctx, line)
    if cause is not None and cause.cls == "counted":
        _attend(s.ctx, cause, run)


def _run(s, rec, run, entry, v):
    """Run the entry, hold or undo once (design §16.4 step 7), then the reach check or the log line and result."""
    ctx = s.ctx
    s.ran = True
    state.note_run(ctx, entry.id)
    logs.write_result(ctx, run, action=entry.id, state="running")
    line = _line(s, rec, run, v)
    try:
        out = run_entry(ctx, entry, run, ctx.script_limit_s, log_path(ctx, run))
    except Refusal as r:              # the script changed between the check and the run: nothing ran
        line.update({"outcome": "refused", "reason": r.reason, "class": r.cls})
        logs.append_run(ctx, line)
        logs.write_result(ctx, run, state="stopped", sentence=r.sentence)
        _attend(ctx, r, run)
        return
    line.update({"exit": out.exit, "secs": out.secs, "staged": out.staged, "timed_out": out.timed_out,
                 "undo_ran": False})
    if out.exit == 0 and not out.timed_out:
        reach.supersede(ctx, entry, s.loaded)
        line["outcome"] = "done"
        if entry.can_cut_you_off:
            reach.begin(ctx, run, entry, rec["seen"], dict(line, started=iso(ctx, ctx.now())))
            return
        logs.append_run(ctx, line)
        logs.write_result(ctx, run, state="done")
        return
    if not out.staged:
        line["outcome"] = "stopped"
        logs.append_run(ctx, line)
        logs.write_result(ctx, run, state="stopped", sentence=sentence("stopped", entry.title))
        return
    line["outcome"] = "failed"
    if entry.undo is None:
        logs.append_run(ctx, line)
        logs.write_result(ctx, run, state="failed", sentence=sentence("not-back", entry.title) + ".",
                          undo_ran=False)
        logs.attention(ctx, "not-back", sentence("not-back", entry.title), run)
        return
    undo_ran, undo_ok, undo_out = _undo(ctx, s.loaded.actions[entry.undo], run)
    line.update({"undo_ran": undo_ran, "undo_ok": undo_ok})
    if undo_out is not None:
        line["undo"] = {"run": undo_run_id(run), "action": entry.undo, "exit": undo_out.exit, "secs": undo_out.secs,
                        "timed_out": undo_out.timed_out}
    logs.append_run(ctx, line)
    logs.write_result(ctx, run, state="failed", undo_ran=undo_ran, undo_ok=undo_ok,
                      sentence=sentence("failed-undone" if undo_ok else "failed-undo-failed", entry.title))
    if not undo_ok:
        logs.attention(ctx, "undo-failed", sentence("undo-failed", entry.title), run)


def _undo(ctx, undo, run):
    """The undo once, with its own limit (ctx.undo_limit_s). (ran, ok, ScriptOutcome or None); ran is False for an
    undo whose script changed or could not start (review N7)."""
    urun = undo_run_id(run)
    try:
        out = run_entry(ctx, undo, urun, ctx.undo_limit_s, log_path(ctx, urun))
    except Refusal:
        return False, False, None
    return out.started, out.started and out.exit == 0 and not out.timed_out, out


# --- a start that found lib/lock held ------------------------------------------------------------------------------

def _busy_start(ctx):
    """Answer every entry this start takes `busy` (D10 as amended in plan v6). A flood (more than 50 waiting or more
    than 200 in quarantine) is left in the inbox for the start that next holds lib/lock, which applies the flood rule
    (review M1). Each entry takes a slot of the hourly cap first (state.open_slot, shared with every start; past it,
    quarantined unopened with one `open-cap` line). The file rules and the strict parse still quarantine; a check-in
    is refused busy (the key page sends it again); a run or check-key record is checked up to its signature
    (webauthn.signed_exp) when root's files are in order, its nonce spent with that exp or none, then refused busy.
    Touches no state but the nonces, the hourly cap, the readable files of logs.py and quarantine. Holds lib/busy-lock
    from the take to the last answer, so leftovers leave its entries alone (re-check notes, item 4); when the lock
    does not come within ctx.lock_wait_s it takes nothing. An entry gone meanwhile is skipped."""
    inbox = ctx.run / "inbox"
    if take.count(inbox) > take.TAKE_LIMIT or take.quarantine_count(ctx) > take.FLOOD_QUARANTINE:
        return 0
    with state.busy_lock(ctx) as held:
        if held:
            _answer_busy(ctx, inbox)
    return 0


def _answer_busy(ctx, inbox):
    loaded, problem = load_or_problem(ctx)
    s = _Start(ctx, loaded, problem)
    taken = take.take(ctx, inbox, "busy")
    for path in taken.paths:
        if not _opened(s, path):
            continue
        try:
            data = take.open_entry(ctx, path)
        except FileNotFoundError:
            continue
        if isinstance(data, take.Bad):
            take.quarantine(ctx, path, data)
            continue
        try:
            rec = parse_record(data)
        except Malformed:
            take.quarantine(ctx, path, None, "malformed", data=data)
            continue
        r = refusal("busy")
        if rec["type"] == "reach":
            logs.append_run(ctx, {"run": rec["run"], "outcome": "refused", "reason": r.reason, "class": r.cls,
                                  "seen": _seen(rec)})
            _unlink(path)
            continue
        run = canon.run_id(rec["nonce"])
        exp = webauthn.signed_exp(rec, loaded.keys, loaded.cfg) if loaded is not None else None
        try:
            state.spend_nonce(ctx, rec["nonce"], exp)
        except (state.Busy, state.Broken, OSError) as e:
            _spend_failed(s, rec, run, e, None)
            continue
        line = _line(s, rec, run)
        line.update({"outcome": "refused", "reason": r.reason, "class": r.cls, "signed": exp is not None})
        logs.append_run(ctx, line)
        logs.write_result(ctx, run, action=rec["action"], state="refused", sentence=r.sentence,
                          **{"class": r.cls})
        _unlink(path)
    _cap_line(ctx, s.over_cap)


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
