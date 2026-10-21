"""zeus deploy-run <id> (docs/design.md §5; issue #8): the deploy itself, started detached by `zeus approve` under
systemd-run. One at a time (a lock file under the data folder). It runs only a record that was approved and whose
approval is a signed line in the log; it fetches main of the allowlisted url again, checks the commit is on it, checks
out exactly that commit (detached, hooks off, no submodules), runs that commit's scripts/deploy.sh --no-pull, waits for
the health check from the fixed list, and records the result. A failure before ZEUS_STAGE=install holds (nothing was
changed); one after it puts back what was live before, once (docs/design.md §6, issue #9)."""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from zeus import approve, config, log, queue, version

STAGE_LINE = "ZEUS_STAGE=install"
HISTORY = 10
HEALTH_POLL_S = 1.0


# ---- one deploy at a time ------------------------------------------------------------------------------------------
class Lock:
    """An exclusive lock on <data>/lock for the whole run; a second deploy-run waits for the first."""
    def __init__(self, path):
        self.path, self.f = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a+")
        try:
            import fcntl
            fcntl.flock(self.f.fileno(), fcntl.LOCK_EX)
        except ImportError:                              # a Windows PC running the tests
            import msvcrt
            while True:
                try:
                    msvcrt.locking(self.f.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.2)
        return self

    def __exit__(self, *exc):
        self.f.close()


# ---- the run log -----------------------------------------------------------------------------------------------------
class RunLog:
    """log/deploy-<id>.log: Zeus's own steps as "zeus: ..." lines, then the script's output as it comes."""
    def __init__(self, store, rid):
        self.path = store.data / "log" / ("deploy-%s.log" % rid)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = open(self.path, "a", encoding="utf-8")

    def zeus(self, text):
        self.f.write("zeus: %s %s\n" % (queue.stamp(queue.now_local()), text))
        self.f.flush()

    def raw(self, line):
        self.f.write(line if line.endswith("\n") else line + "\n")
        self.f.flush()

    def close(self):
        self.f.close()


# ---- state/<repo>.json -----------------------------------------------------------------------------------------------
def state_path(store, repo_key):
    return store.data / "state" / (repo_key.replace("/", "--") + ".json")


def read_state(store, repo_key):
    return store.deployed(repo_key) or {"repo": repo_key, "deployed": None, "previous": None, "history": []}


def write_state(store, repo_key, st):
    p = state_path(store, repo_key)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def record_deployed(store, rec, now):
    """The new deployed entry; the old one becomes previous and goes on top of history (10 kept)."""
    st = read_state(store, rec["repo"])
    entry = {"sha": rec["sha"], "version": rec["version"], "at": queue.stamp(now), "id": rec["id"],
             "approval": (rec.get("approval") or {}).get("by")}
    old = st.get("deployed")
    if old and old.get("sha") != rec["sha"]:
        st["previous"] = old
        st["history"] = ([old] + [h for h in st.get("history") or [] if h.get("sha") != old.get("sha")])[:HISTORY]
    st["repo"], st["deployed"] = rec["repo"], entry
    write_state(store, rec["repo"], st)
    return st


# ---- the steps -------------------------------------------------------------------------------------------------------
class Failed(Exception):
    """A step failed; the message is the plain-sentence reason for the card. after_install: what runs may have changed
    (the script passed ZEUS_STAGE=install, timed out, or the health check never passed), so the previous goes back."""
    def __init__(self, message, after_install=False):
        super().__init__(message)
        self.after_install = after_install


def approved_in_log(store, rec):
    """The record's approval is a signed line in the approvals log with the same id, sha, action and sig."""
    sig = (rec.get("approval") or {}).get("sig")
    if not sig:
        return False
    for _, _, entry in log.entries(store):
        if entry and entry.get("sig") == sig and entry.get("id") == rec["id"] and entry.get("sha") == rec["sha"] \
                and entry.get("action") == rec.get("action", "deploy") and entry.get("action") in ("deploy", "rollback"):
            return log.check(entry)
    return False


def check_health_config(repo):
    """The same rule check-config applies, again at run time: one of http (127.0.0.1), service, exec (fixed list)."""
    h = repo.health if isinstance(repo.health, dict) else {}
    shapes = [s for s in config.HEALTH_SHAPES if s in h]
    if len(shapes) != 1 or set(h) - set(config.HEALTH_SHAPES):
        raise Failed("the health check of %s is not one of http, service or exec; nothing was run." % repo.key)
    shape, value = shapes[0], h[shapes[0]]
    if shape == "http" and not (isinstance(value, str) and config.HEALTH_HTTP.match(value)):
        raise Failed("health.http %r is not a URL on 127.0.0.1; nothing was run." % (value,))
    if shape == "service" and not (isinstance(value, str) and config.UNIT.match(value)):
        raise Failed("health.service %r is not a unit name; nothing was run." % (value,))
    if shape == "exec" and value not in config.HEALTH_EXEC:
        raise Failed("health.exec %r is not one of %s; Zeus never runs a command named in config, so nothing was run."
                     % (value, ", ".join(config.HEALTH_EXEC)))
    return shape, value


def prepare_clone(store, repo, out):
    """The clone at path: made on the first deploy (no checkout, hooks off), else the one that is there."""
    path = repo.path
    if (path / ".git").exists():
        return path
    if path.exists() and any(path.iterdir()):
        raise Failed("%s exists, is not empty and is not a git clone; Zeus deploys only into his own clone."
                     % config.short(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    out("making the clone %s" % config.short(path))
    code, _, err = queue.git(["clone", "--quiet", "--no-checkout", repo.url, str(path)], env=queue.git_env(repo),
                             timeout=600)
    if code != 0:
        raise Failed("could not clone %s: %s" % (repo.url, err.strip() or "git exit %d" % code))
    return path


def checkout(store, repo, rec, out, on_main=True):
    """Fetch main again, check the commit is on it, refuse submodules, check out the commit detached. on_main=False for
    a commit that was approved and deployed before (a rollback, a restore): it only has to exist in the clone."""
    path = prepare_clone(store, repo, out)
    fetch = queue.Fetch(store, repo)
    if not fetch.run():
        raise Failed(fetch.error)
    if fetch.where != path:
        raise Failed("the fetch did not go into the clone %s" % config.short(path))
    if on_main and not fetch.on_main(rec["sha"]):
        raise Failed("commit %s is not on %s of %s; nothing was run." % (rec["sha"][:7], config.BRANCH, rec["repo"]))
    code, tree, err = queue.git(["ls-tree", "-r", rec["sha"]], cwd=path)
    if code != 0:
        raise Failed("cannot read the tree of %s: %s" % (rec["sha"][:7], err.strip()))
    if any(l.startswith("160000 ") for l in tree.splitlines()) or "\t.gitmodules" in tree:
        raise Failed("commit %s uses submodules, which Zeus refuses; nothing was run." % rec["sha"][:7])
    if not any(l.endswith("\t" + config.DEPLOY_SCRIPT) for l in tree.splitlines()):
        raise Failed("commit %s has no %s; nothing was run." % (rec["sha"][:7], config.DEPLOY_SCRIPT))
    code, _, err = queue.git(["checkout", "--quiet", "--detach", rec["sha"]], cwd=path)
    if code != 0:
        raise Failed("could not check out %s in %s (%s). Nobody should edit Zeus's clone; look at `git status` there."
                     % (rec["sha"][:7], config.short(path), err.strip()))
    code, ver, _ = queue.git(["show", "%s:VERSION" % rec["sha"]], cwd=path)
    out("checked out %s (detached) in %s" % (rec["sha"][:7], config.short(path)))
    return path, ver.strip() if code == 0 else None


def run_script(path, rec, timeout, runlog):
    """bash scripts/deploy.sh --no-pull in the clone. Returns (exit code or None on timeout, install stage reached)."""
    env = dict(os.environ, ZEUS_ID=rec["id"], ZEUS_SHA=rec["sha"], ZEUS_PREVIOUS=rec.get("previous") or "")
    bash = shutil.which("bash") or "bash"          # which(): on a PC, PATH's Git Bash, not System32's WSL bash
    kw = {"start_new_session": True} if os.name != "nt" else {}
    p = subprocess.Popen([bash, config.DEPLOY_SCRIPT, "--no-pull"], cwd=str(path), env=env, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True, encoding="utf-8",
                         errors="replace", **kw)
    stage = {"install": False}

    def pump():
        for line in p.stdout:
            runlog.raw(line)
            if line.strip() == STAGE_LINE:
                stage["install"] = True
    t = threading.Thread(target=pump, daemon=True)
    t.start()
    try:
        code = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(p.pid, 9)
            else:
                p.kill()
        except OSError:
            pass
        p.wait()
        code = None
    t.join(timeout=5)
    return code, stage["install"]


def health_once(shape, value, want_version):
    """One try of the health check. Returns None when healthy, else why not."""
    if shape == "http":
        try:
            with urllib.request.urlopen(value, timeout=5) as r:
                body = r.read(65536)
                status = r.status
        except Exception as e:                                  # refused, timeout, HTTP error: not healthy yet
            return "GET %s: %s" % (value, e)
        if status != 200:
            return "GET %s answered %d" % (value, status)
        try:
            doc = json.loads(body)
        except ValueError:
            doc = None
        if isinstance(doc, dict) and "version" in doc and want_version and str(doc["version"]) != want_version:
            return "GET %s says version %s, the commit is %s" % (value, doc["version"], want_version)
        return None
    if shape == "service":
        cmd = approve.command("ZEUS_SYSTEMCTL", "systemctl") + ["--user", "is-active", "--quiet", value]
    else:                                                       # exec, from the fixed list only
        agent = value.split()[0]
        cmd = approve.command("ZEUS_HEALTH_" + agent.upper(), agent) + ["check-config"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return "%s: %s" % (" ".join(cmd[-3:]), e)
    return None if r.returncode == 0 else "%s exited %d" % (value if shape == "exec" else "is-active " + value,
                                                            r.returncode)


def wait_healthy(shape, value, want_version, wait_s, runlog):
    deadline = time.monotonic() + wait_s
    why = health_once(shape, value, want_version)
    while why and time.monotonic() < deadline:
        time.sleep(HEALTH_POLL_S)
        why = health_once(shape, value, want_version)
    runlog.zeus("health: %s" % ("ok" if why is None else "not passing: " + why))
    return why


# ---- the run ---------------------------------------------------------------------------------------------------------
def deploy_line(store, rec, outcome, reason, stage, now):
    try:
        log.append(store, {"t": queue.stamp(now), "id": rec["id"], "repo": rec["repo"], "sha": rec["sha"],
                           "version": rec["version"], "outcome": outcome, "reason": reason, "install_stage": stage,
                           "zeus": version()}, kind="deploys", when=now)
    except log.LogError:
        pass                     # the record and the run log still say what happened


def run(store, cfg, rid, out=None):
    """The whole deploy (or hand rollback) of one approved record. Returns 0 deployed · 1 failed, rolled back or refused.
    A failure before ZEUS_STAGE=install changed nothing: the record is failed and Zeus holds. A failure after it, a
    timeout, or a health check that never passes puts back what was live before this run (state.deployed), once.
    Zeus never retries on his own."""
    out = out or (lambda s: None)
    with Lock(store.data / "lock"):
        rec = store.read(rid)
        if rec is None:
            out("zeus deploy-run: no request %s." % rid)
            return 1
        if rec["state"] != "approved":
            out("zeus deploy-run: %s is %s, not approved; nothing was run." % (rid, rec["state"]))
            return 1
        if not approved_in_log(store, rec):
            out("zeus deploy-run: %s has no signed approval in the log; nothing was run." % rid)
            return 1
        runlog = RunLog(store, rid)
        say = lambda t: (runlog.zeus(t), out(t))
        repo = cfg.repos.get(rec["repo"]) if cfg else None
        rollback = rec.get("action") == "rollback"
        stage, started = False, queue.now_local()
        live = read_state(store, rec["repo"]).get("deployed")      # what runs now; the restore target on a failure
        rec["state"] = "deploying"
        rec["deploy"] = dict(rec.get("deploy") or {}, run_started=queue.stamp(started),
                             log=config.short(runlog.path))
        store.write(rec)
        try:
            if repo is None:
                raise Failed("%s is not in repos.json; nothing was run." % rec["repo"])
            shape, value = check_health_config(repo)
            if rollback:
                prev = read_state(store, rec["repo"]).get("previous") or {}
                if prev.get("sha") != rec["sha"]:
                    raise Failed("the previous commit of %s is no longer %s (something was deployed since the rollback "
                                 "was approved); nothing was run." % (rec["repo"], rec["sha"][:7]))
                say("rolling %s back to %s (%s)" % (rec["repo"], rec["version"] or "", rec["sha"][:7]))
            else:
                say("deploying %s %s (%s)" % (rec["repo"], rec["version"] or "", rec["sha"][:7]))
            path, ver = checkout(store, repo, rec, say, on_main=not rollback)
            stage = attempt(path, rec, ver, shape, value, cfg, runlog, say)
        except Failed as e:
            now = queue.now_local()
            stage = stage or e.after_install
            rec["deploy"].update(finished=queue.stamp(now), install_stage=stage)
            name = rec["repo"].split("/")[-1]
            this = rec["version"] or rec["sha"][:7]
            if not stage:
                rec["state"], rec["reason"] = "failed", str(e)
                rec["deploy"]["outcome"] = "failed"
                store.write(rec)
                say("failed before the install began, so nothing was changed; holding: %s" % e)
                deploy_line(store, rec, "failed", str(e), stage, now)
                approve.alert_write("%s %s failed before the install; nothing was changed: %s" % (name, this, e), now)
                runlog.close()
                return 1
            say("failed after the install had begun: %s" % e)
            back = restore(store, repo, rec, live, cfg, runlog, say)
            now = queue.now_local()
            if back is True:
                old = live.get("version") or live["sha"][:7]
                rec["state"] = "rolled-back"
                rec["reason"] = "%s; %s was put back and is healthy." % (str(e).rstrip("."), old)
                rec["deploy"]["outcome"] = "rolled-back"
                rec["deploy"]["restored"] = {"sha": live["sha"], "version": live.get("version")}
                store.write(rec)
                deploy_line(store, rec, "rolled-back", rec["reason"], stage, now)
                approve.alert_write("%s %s failed and %s was put back: %s" % (name, this, old, e), now)
                say("rolled back: %s %s runs again" % (rec["repo"], old))
                runlog.close()
                return 1
            if back is None:
                message = "%s %s failed and there is no earlier approved commit to put back" % (name, this)
            else:
                message = "%s %s failed and %s could not be restored" % (name, this, live.get("version") or live["sha"][:7])
            rec["state"], rec["reason"] = "failed", "%s; %s." % (str(e).rstrip("."), message[len(name) + 1:])
            rec["deploy"]["outcome"] = "failed"
            store.write(rec)
            deploy_line(store, rec, "failed", rec["reason"], stage, now)
            approve.alert_write(message, now)
            say("failed: %s. Nothing more is tried; look at this log and the Throne." % message)
            runlog.close()
            return 1
        now = queue.now_local()
        if rollback:
            record_rolled_back(store, rec, now)
        else:
            record_deployed(store, rec, now)
        rec["state"] = "deployed"
        rec.pop("reason", None)
        rec["deploy"].update(finished=queue.stamp(now), outcome="deployed", install_stage=stage)
        store.write(rec)
        deploy_line(store, rec, "deployed", None, stage, now)
        approve.alert_clear()
        say("%s %s %s (%s)" % ("rolled back to" if rollback else "deployed", rec["repo"], rec["version"] or "",
                               rec["sha"][:7]))
        runlog.close()
        return 0


def attempt(path, rec, ver, shape, value, cfg, runlog, say):
    """Run the script on what is checked out and wait for health. Returns the install stage; raises Failed."""
    say("running %s --no-pull" % config.DEPLOY_SCRIPT)
    code, stage = run_script(path, rec, cfg.deploy_timeout_s, runlog)
    if code is None:
        raise Failed("%s did not finish in %g s and was stopped" % (config.DEPLOY_SCRIPT, cfg.deploy_timeout_s)
                     + (", after the install had begun." if stage else "."), after_install=True)
    if code != 0:
        raise Failed("%s exited %d %s." % (config.DEPLOY_SCRIPT, code, "after the install had begun" if stage
                     else "before the install began, so nothing was changed"), after_install=stage)
    why = wait_healthy(shape, value, ver, cfg.health_wait_s, runlog)
    if why:
        raise Failed("the health check did not pass in %g s: %s." % (cfg.health_wait_s, why), after_install=True)
    return stage


def restore(store, repo, rec, live, cfg, runlog, say):
    """Put back what was live before this run: its commit, its own deploy script, its health. True when it came back,
    None when there was nothing to put back, False when that failed too. Tried once; never again on its own."""
    if not live or not live.get("sha") or live["sha"] == rec["sha"]:
        say("there is no earlier approved commit of %s to put back." % rec["repo"])
        return None
    old = live.get("version") or live["sha"][:7]
    say("putting back %s (%s), the commit that was live before this run, because the install of %s had begun"
        % (old, live["sha"][:7], rec["version"] or rec["sha"][:7]))
    back = {"id": rec["id"], "sha": live["sha"], "previous": rec["sha"], "repo": rec["repo"]}
    try:
        shape, value = check_health_config(repo)
        path, ver = checkout(store, repo, back, say, on_main=False)
        attempt(path, back, ver, shape, value, cfg, runlog, say)
    except Failed as e:
        say("putting back %s failed too: %s" % (old, e))
        return False
    return True


def record_rolled_back(store, rec, now):
    """A hand rollback ran: previous becomes deployed and the stack moves one step (what it rolled back from is gone)."""
    st = read_state(store, rec["repo"])
    hist = [h for h in st.get("history") or [] if h.get("sha") != rec["sha"]]
    entry = dict(st.get("previous") or {}, at=queue.stamp(now), id=rec["id"],
                 approval=(rec.get("approval") or {}).get("by"))
    st["deployed"] = entry
    st["previous"] = hist[0] if hist else None
    st["history"] = hist[:HISTORY]
    write_state(store, rec["repo"], st)
    return st


def main(args):
    cfg = config.check(getattr(args, "file", None))
    if cfg.errors:
        print("zeus deploy-run: %s has errors; run `zeus check-config`." % config.short(cfg.path), file=sys.stderr)
        cfg = None
    return run(queue.Store(), cfg, args.id, out=print)
