"""The reach check (design §16.4 step 8, private design 5.6, plan Task 8, contract §8b and §8e).

After a successful run of an entry that can cut the phone off, root writes lib/reach/<run>.json (root 600) with the
deadline (now + 600 s) and starts zeus-root-reach@<run>.timer, a once-a-minute heartbeat; the deadline itself lives
on disk. A check-in (a `reach` record from the approving login and device, before the deadline) keeps the change.
Without one, at the deadline the paired undo runs by itself: at the tick, at the next runner start if a timer was
lost, or at boot. A running game (when the undo has `game_check`) delays it by at most config's `reach_game_max_s`;
a check-in after the deadline keeps nothing, also during that delay or while root's own files are broken (review N1).

One log line per try, written when it is final (D6): the run's line waits in its reach file (`pending`) and is
written with `reach` = kept, undone or superseded; a put-back's undo writes its own line too. Callers hold
state.lock(ctx): the runner for a whole start, tick and boot take it themselves (boot starts the timers first, without
it). A wait that is kept or superseded says so in its reach file (`final`) before its line is written, so a crash
before the move to reach/done/ neither loses the line nor lets a put-back follow it: whoever finds that file next
finishes it (the line only if the log does not hold it yet; review S4). A waiting file stays in place while a
put-back's undo runs, so a crash in the middle puts back again at the next tick (the pairs' undos are idempotent);
it moves to reach/done/ once its lines are written.
"""
import contextlib
import os

from . import canon, games, logs, state
from .ctx import iso
from .webauthn import Refusal

REACH_S = 600
REMIND_S = 300
SYSTEMCTL = "/usr/bin/systemctl"
TIMER = "zeus-root-reach@%s.timer"
TIMER_TIMEOUT_S = 30
# canon.require wants exactly these keys, so a wait left open by an older zeus-root would not read back after an
# upgrade that changes them (boot and every runner start then stop on state.Broken): a change ships with a migration.
DOC_SCHEMA = {"run": str, "action": str, "undo": str, "node_id": str, "login": str, "deadline": (int, float),
              "remind_at": (int, float), "game_wait_start": (int, float, type(None)), "pending": dict,
              "final": (str, type(None))}
FINALS = ("kept", "superseded")


def _folder(ctx):
    return ctx.lib / "reach"


def _path(ctx, run):
    if not isinstance(run, str) or not logs.RUN_RE.fullmatch(run):
        raise ValueError("not a run id")
    return _folder(ctx) / (run + ".json")


def _load(path):
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        doc = canon.strict_loads(raw)
        canon.require(doc, DOC_SCHEMA)
    except canon.Malformed:
        raise state.Broken(path.name + " does not read back") from None
    if path.name != doc["run"] + ".json":
        raise state.Broken(path.name + " names another run")
    if doc["final"] is not None and doc["final"] not in FINALS:
        raise state.Broken(path.name + " has an unknown final")
    return doc


def _save(ctx, doc):
    state.write_file(_path(ctx, doc["run"]), canon.canonical(doc) + b"\n", 0o600)


def _names(ctx):
    """The run id of every wait in reach/ by its file name alone (r + 20 hex + .json, a plain file): nothing is
    parsed, so a file that does not read back cannot keep the others' timers from starting at boot."""
    with os.scandir(_folder(ctx)) as it:
        return sorted(e.name[:-5] for e in it if e.name.endswith(".json") and logs.RUN_RE.fullmatch(e.name[:-5])
                      and e.is_file(follow_symlinks=False))


def waiting(ctx):
    """[(path, doc)] of every run that waits for a check-in, oldest deadline first. Raises state.Broken on the first
    file that does not read back."""
    out = []
    for run in _names(ctx):
        doc = _load(_path(ctx, run))
        if doc is not None:
            out.append((_path(ctx, run), doc))
    return sorted(out, key=lambda pd: (pd[1]["deadline"], pd[1]["run"]))


def _timer(ctx, verb, run):
    """systemctl start or stop of the run's timer (the run id is root's own, checked against RUN_RE)."""
    _path(ctx, run)
    return ctx.cmd([SYSTEMCTL, verb, TIMER % run], TIMER_TIMEOUT_S).code == 0


def _close(ctx, path, doc):
    """Move a finished wait to reach/done/ (made on first use, root 700) and stop its timer."""
    done = _folder(ctx) / "done"
    os.makedirs(done, mode=0o700, exist_ok=True)
    os.replace(path, done / path.name)
    state._fsync_dir(_folder(ctx))
    _timer(ctx, "stop", doc["run"])


def begin(ctx, run, entry, seen, pending_line):
    """Start the wait for a run that just succeeded: the reach file, the timer, the result `waiting` with its deadline
    and the reminder's time (deadline - 300 s). A timer that does not start writes a `watcher-failed` line: the wait
    then ends at the next runner start or at boot."""
    now = ctx.now()
    doc = {"run": run, "action": entry.id, "undo": entry.undo, "node_id": seen["node_id"], "login": seen["login"],
           "deadline": now + REACH_S, "remind_at": now + REMIND_S, "game_wait_start": None, "pending": pending_line,
           "final": None}
    logs.assert_no_ids(doc)
    _save(ctx, doc)
    logs.write_result(ctx, run, action=entry.id, state="waiting", deadline=iso(ctx, doc["deadline"]),
                      remind_at=iso(ctx, doc["remind_at"]))
    if not _timer(ctx, "start", run):
        logs.attention(ctx, "watcher-failed", "The reach check's timer for %s did not start; it is put back at the "
                       "next start or restart unless your phone checks in" % entry.title, run)


def _final_line_written(ctx, run):
    """Whether the run log already holds the run's line with its reach outcome (a finish that crashed after it)."""
    return any(obj is not None and obj.get("run") == run and "reach" in obj
               for _, _, obj, _ in logs.iter_run_lines(ctx))


def _finish(ctx, path, doc, final, loaded):
    """End a wait that was kept or superseded: `final` in the reach file first (one atomic write), then the run's
    line, the result, the move to reach/done/ and the timer's stop. A file that already says final keeps what it says
    and is only finished: its line is written only when the log does not hold it yet (review S4)."""
    if doc["final"] is None:
        doc = dict(doc, final=final)
        _save(ctx, doc)
        written = False
    else:
        written = _final_line_written(ctx, doc["run"])
    if not written:
        logs.append_run(ctx, dict(doc["pending"], reach=doc["final"]))
    if doc["final"] == "kept":
        logs.write_result(ctx, doc["run"], state="done", deadline=None, remind_at=None)
    else:
        logs.write_result(ctx, doc["run"], state="superseded", deadline=None, remind_at=None,
                          sentence="%s was followed by a newer run, so it is not put back."
                          % _title(loaded, doc["action"]))
    _close(ctx, path, doc)


def check_in(ctx, rec, loaded=None):
    """A check-in record (D3): True when its run waits, its deadline has not passed, and seen.node_id and seen.login
    are the approver's; the run's line is written with reach `kept`, the result says done, the wait ends. False for
    anything else (the runner logs it as garbage); the run still waits, and a late one is put back (review N1). A wait
    a crash left half finished is finished as its file says."""
    path = _path(ctx, rec["run"])
    doc = _load(path)
    if doc is None or rec["seen"]["node_id"] != doc["node_id"] or rec["seen"]["login"] != doc["login"]:
        return False
    if doc["final"] is None and ctx.now() >= doc["deadline"]:
        return False
    _finish(ctx, path, doc, "kept", loaded)
    return doc["final"] in (None, "kept")


def supersede(ctx, entry, loaded):
    """After a done run of `entry`: a waiting check of its pair, or of the same action, is superseded (S10), so a
    put-back never undoes a newer run and at most one check waits per pair."""
    names = {entry.id, entry.undo} - {None}
    for path, doc in waiting(ctx):
        if doc["action"] in names:
            _finish(ctx, path, doc, "superseded", loaded)


def _title(loaded, action):
    a = loaded.actions.get(action) if loaded is not None else None
    return a.title if a is not None else action


def _game_minutes(loaded):
    return max(1, round(loaded.cfg.reach_game_max_s / 60))


def _handle(ctx, path, doc, loaded, after_restart):
    """One waiting check at this moment: a file that says final is finished (never put back); otherwise nothing
    before the deadline; after it, put back, except while a game runs (the undo has game_check) for at most
    reach_game_max_s from the first time one was seen running. True when a put-back ran."""
    if doc["final"] is not None:
        _finish(ctx, path, doc, doc["final"], loaded)
        return False
    now = ctx.now()
    if now < doc["deadline"]:
        return False
    if after_restart:
        put_back(ctx, path, doc, loaded, "no check-in, after a restart")
        return True
    undo = loaded.actions.get(doc["undo"])
    if undo is not None and undo.game_check and loaded.cfg.reach_game_max_s > 0:
        if games.running(ctx, loaded.cfg.games) is not None:
            started = doc["game_wait_start"]
            if started is None:
                started = now
                _save(ctx, dict(doc, game_wait_start=now))
            if now - started < loaded.cfg.reach_game_max_s:
                return False
            put_back(ctx, path, doc, loaded, "no check-in, after %d minutes of game" % _game_minutes(loaded))
            return True
    put_back(ctx, path, doc, loaded, "no check-in")
    return True


def put_back(ctx, path, doc, loaded, reason):
    """Run the paired undo for a run nobody checked in for (the caller holds the lock): its own run id "u"+run[1:],
    its own limit, the same checks as any run (the script's hash; never a game check here, the caller decided).
    Lines: the run's pending line with reach `undone` (`undo-failed` when the undo did not run or failed, so the line
    never says undone while the result says failed), and the undo's own line with the reason. Result `undone` (or
    `failed` when the undo failed). One attention line: `put-back`, or `undo-failed` when it failed."""
    from . import runner
    run = doc["run"]
    urun = runner.undo_run_id(run)
    title = _title(loaded, doc["action"])
    undo = loaded.actions.get(doc["undo"])
    out, ran = None, False
    if undo is not None:
        try:
            out = runner.run_entry(ctx, undo, urun, ctx.undo_limit_s, runner.log_path(ctx, urun))
            ran = out.started             # an undo that could not start did not run (review N7)
        except Refusal:
            pass
    ok = ran and out.exit == 0 and not out.timed_out
    logs.append_run(ctx, dict(doc["pending"], reach="undone" if ok else "undo-failed"))
    line = {"run": urun, "of": run, "action": doc["undo"], "outcome": "undone" if ok else "failed", "reason": reason,
            "undo_ran": ran}
    if out is not None:
        line.update({"exit": out.exit, "secs": out.secs, "staged": out.staged, "timed_out": out.timed_out})
    logs.append_run(ctx, line)
    why = "your phone did not check in" + ("" if reason == "no check-in" else " (%s)" % reason[len("no check-in, "):])
    if ok:
        logs.write_result(ctx, run, state="undone", deadline=None, remind_at=None, undo_ran=True, undo_ok=True,
                          sentence="%s was put back: %s." % (title, why))
        logs.attention(ctx, "put-back", "%s was put back: %s" % (title, why), run)
    else:
        logs.write_result(ctx, run, state="failed", deadline=None, remind_at=None, undo_ran=ran, undo_ok=False,
                          sentence="%s could not be put back after %s: its undo failed." % (title, why))
        logs.attention(ctx, "undo-failed", "%s could not be put back: its undo failed" % title, run)
    _close(ctx, path, doc)


def due(ctx, loaded):
    """Every waiting check whose deadline passed (a lost timer), as a tick would; the runner calls it under its lock
    before it takes the inbox. Returns how many put-backs ran. A reach file that does not read back raises
    state.Broken: the start fails (Elaia's watch shows it) and every other wait's own timer still ticks."""
    return sum(1 for path, doc in waiting(ctx) if _handle(ctx, path, doc, loaded, False))


@contextlib.contextmanager
def _locked_or_none(ctx):
    lock = state.lock(ctx)
    try:
        lock.__enter__()
    except state.Busy as e:
        if type(e) is not state.Busy:
            raise
        yield False
        return
    try:
        yield True
    finally:
        lock.__exit__(None, None, None)


def tick(ctx, run):
    """zeus-root reach-tick <run>, once a minute from its timer. A run that no longer waits stops its timer; one whose
    file says final is finished. The lock held elsewhere (a start, a command) means: nothing now, the next tick tries
    again. Root's own files broken: no put-back runs (nothing runs, design §16.3); the next tick tries again. Returns
    the exit code."""
    from . import runner
    path = _path(ctx, run)
    with _locked_or_none(ctx) as held:
        if not held:
            return 0
        doc = _load(path)
        if doc is None:
            _timer(ctx, "stop", run)
            return 0
        if doc["final"] is None and ctx.now() < doc["deadline"]:
            return 0
        loaded, problem = runner.load_or_problem(ctx)
        if doc["final"] is None and problem is not None:
            logs.attention(ctx, "file-rules", "Root's own files break the ownership or mode rules (%s); a put-back "
                           "waits until they are fixed" % problem, run)
            return 1
        _handle(ctx, path, doc, loaded, False)
    return 0


def _title_of(ctx, loaded, run):
    """The action's title for a run's wait, or the run id when its file does not read back (or is gone)."""
    try:
        doc = _load(_path(ctx, run))
    except state.Broken:
        doc = None
    return _title(loaded, doc["action"]) if doc is not None else run


def boot(ctx):
    """zeus-root boot, once at every boot. First, without lib/lock, every wait gets its timer started again by its
    file name, so a tick comes whatever this boot finds: root's own files broken, the lock held elsewhere (review S3),
    a wait that says final (its tick finishes it), or a reach file that does not read back (its own tick fails, the
    others put back). Then, under lib/lock, with root's files loaded again (they may have broken while the timers
    started; review re-check M1): leftovers in work/ to quarantine (their nonce spent); every wait whose file says
    final is finished; every wait whose deadline passed is put back at once ("no check-in, after a restart": no
    check-in could be trusted across the restart, and no game runs this early). Root's own files broken: no put-back
    runs, the ticks put back once they are fixed. Returns the exit code: 1 when the lock was held elsewhere or root's
    files are broken; raises state.Broken on a reach file that does not read back."""
    from . import runner
    early, _ = runner.load_or_problem(ctx)          # only for the titles of watcher-failed lines
    for run in _names(ctx):
        if not _timer(ctx, "start", run):
            logs.attention(ctx, "watcher-failed", "The reach check's timer for %s did not start again after a "
                           "restart; it is put back at the next start unless your phone checks in"
                           % _title_of(ctx, early, run), run)
    with _locked_or_none(ctx) as held:
        if not held:
            return 1
        loaded, problem = runner.load_or_problem(ctx)
        runner.leftovers(ctx, loaded)
        if problem is not None:
            logs.attention(ctx, "file-rules", "Root's own files break the ownership or mode rules (%s); nothing runs "
                           "until they are fixed" % problem, None)
            return 1
        for path, doc in waiting(ctx):
            if doc["final"] is not None or ctx.now() >= doc["deadline"]:
                _handle(ctx, path, doc, loaded, True)
    return 0
