"""Zeus · Throne, the Elaia server plugin `throne` (docs/design.md §9, docs/contract.md).

Reads are file reads under Zeus's data folder (queue/, queue/.done/, state/, log/) and change nothing: a waiting
request past its expiry is shown as expired, and Zeus marks it so himself when he next reads the queue. Every write
runs the zeus command with fixed arguments and returns what Zeus said; Zeus (and Janus behind him) decides. The plugin
never decides, never writes a file and never runs a shell.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ID = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9._-]{1,100}$")
SHA = re.compile(r"^[0-9a-f]{40}$")
WORD = re.compile(r"^[a-z][a-z-]{0,23}$")
REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")   # owner/name: never "..", never a flag
WAITING = ("pending", "held")
RUNNING = ("approved", "deploying")
STAGES = ("fetch", "checkout", "build", "install", "health")
# what each stage leaves in log/deploy-<id>.log (zeus/deploy.py): the stage has begun once its mark is there
MARKS = (("fetch", "zeus: "), ("checkout", "checked out "), ("build", "running scripts/deploy.sh"),
         ("install", "ZEUS_STAGE=install"), ("health", "health: "))
HISTORY = 60          # rows the page gets; everything stays in queue/.done/
TAIL = 80             # log lines the page gets


def data():
    return Path(os.path.expanduser(os.environ.get("ZEUS_DATA") or "~/.local/share/zeus"))


def zeus_bin():
    """The zeus command: $ZEUS_BIN (tests), else on PATH, else ~/.local/bin/zeus (Elaia's unit may have a short PATH)."""
    return os.environ.get("ZEUS_BIN") or shutil.which("zeus") or os.path.expanduser("~/.local/bin/zeus")


def _read(p):
    try:
        doc = json.loads(Path(p).read_text(encoding="utf-8"))
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def _records(folder, limit=None):
    """The records in a folder, newest first (an id starts with its time). Broken files are skipped, never fixed."""
    try:
        paths = sorted(Path(folder).glob("*.json"), reverse=True)
    except OSError:
        return []
    out = []
    for p in paths:
        rec = _read(p)
        if rec and isinstance(rec.get("id"), str):
            out.append(rec)
            if limit and len(out) >= limit:
                break
    return out


def _when(s):
    try:
        t = datetime.fromisoformat(str(s))
        return t if t.tzinfo else t.astimezone()
    except ValueError:
        return None


def repo_keys():
    """The repositories Zeus deploys: repos.json's keys, plus any that have a state file (repos.json may be unreadable)."""
    keys = []
    cfg = _read(os.path.expanduser(os.environ.get("ZEUS_CONFIG") or "~/.config/zeus/repos.json")) or {}
    if isinstance(cfg.get("repos"), dict):
        keys = [k for k in cfg["repos"] if isinstance(k, str) and REPO.match(k)]
    for p in sorted((data() / "state").glob("*--*.json")) if (data() / "state").is_dir() else []:
        k = p.stem.replace("--", "/", 1)
        if REPO.match(k) and k not in keys:
            keys.append(k)
    return keys


def log_path(rid):
    return data() / "log" / ("deploy-%s.log" % rid)


def log_tail(rid, n=TAIL):
    try:
        with open(log_path(rid), encoding="utf-8", errors="replace") as f:
            return f.read().splitlines()[-n:]
    except OSError:
        return []


def stages(rec, lines):
    """The five stages as the log shows them: done, now, todo or failed. The record's state says how it ended."""
    text = "\n".join(lines)
    reached = max([i for i, (_, mark) in enumerate(MARKS) if mark in text], default=-1)
    state = rec.get("state")
    out = []
    for i, name in enumerate(STAGES):
        if state == "deployed":
            s = "done"
        elif state in ("failed", "rolled-back") and i == max(reached, 0):
            s = "failed"
        elif i < reached or (i == reached and state not in RUNNING):
            s = "done"
        elif i == reached or (reached < 0 and i == 0 and state in RUNNING):
            s = "now"
        else:
            s = "todo"
        out.append({"name": name, "state": s})
    return out


def deployed_now():
    """Per repository: what runs (state/<owner>--<name>.json `deployed`) and what a rollback would put back (`previous`)."""
    out = []
    for key in repo_keys():
        st = _read(data() / "state" / (key.replace("/", "--") + ".json")) or {}
        live = st.get("deployed") if isinstance(st.get("deployed"), dict) else None
        prev = st.get("previous") if isinstance(st.get("previous"), dict) else None
        out.append({"repo": key, "deployed": live, "previous": prev})
    return out


def alert():
    a = _read(os.path.expanduser(os.environ.get("ZEUS_ALERT") or "~/.local/state/argus-ui/alerts/zeus.json"))
    return {"level": a.get("level"), "message": str(a.get("message") or "")[:300], "since": a.get("since")} if a else None


def snapshot(now=None):
    """Everything the page shows, from files only. Nothing is written."""
    now = now or datetime.now(timezone.utc).astimezone()
    q = data() / "queue"
    waiting, expired = [], []
    for rec in _records(q):
        if rec.get("state") not in WAITING:
            continue
        exp = _when(rec.get("expires"))
        if exp and exp <= now:          # shown as expired now; Zeus records it when he next lists the queue
            expired.append(dict(rec, state="expired", reason=rec.get("reason") or "expired before anyone approved it"))
        else:
            waiting.append(rec)
    done = _records(q / ".done", limit=HISTORY + 20)
    deploying = []
    for rec in done:
        if rec.get("state") in RUNNING:
            lines = log_tail(rec["id"], 15)
            deploying.append(dict(rec, stages=stages(rec, lines), tail=lines))
    history = expired + [dict(r, has_log=log_path(r["id"]).is_file()) for r in done if r.get("state") not in RUNNING]
    live = deployed_now()
    return {"at": now.isoformat(timespec="seconds"), "line": status_line(waiting, live),
            "waiting": waiting, "deploying": deploying, "deployed": live, "history": history[:HISTORY], "alert": alert()}


def status_line(waiting, live):
    n = len(waiting)
    parts = ["%d deploy%s waiting" % (n, "" if n == 1 else "s") if n else "Nothing waiting"]
    for d in live:
        if d["deployed"]:
            parts.append("%s %s live" % (d["repo"].split("/")[-1], d["deployed"].get("version") or str(d["deployed"].get("sha") or "")[:7]))
    return " · ".join(parts)


def home_status():
    """Elaia's Home (status v2): the line, and "needs you" with a verb-first button while something waits."""
    s = snapshot()
    w = [r for r in s["waiting"] if r.get("state") == "pending"]
    if len(w) == 1:
        action = "Review %s %s" % (str(w[0].get("repo") or "").split("/")[-1], w[0].get("version") or "")
    else:
        action = "Review %d deploys" % len(w)
    return {"line": s["line"], "needs": bool(w), "action": action.strip()[:32] if w else "", "count": len(w)}


# ---- Elaia's entry points --------------------------------------------------------------------------------------------
def get(path, query, ctx):
    if path == "":
        return 200, snapshot()
    if path == "status":
        return 200, home_status()
    if path == "log":
        rid = (query.get("id") or [""])[0]
        if not ID.match(rid):
            return 400, {"ok": False, "msg": "bad id"}
        rec = _read(data() / "queue" / ".done" / (rid + ".json")) or _read(data() / "queue" / (rid + ".json")) or {"state": ""}
        lines = log_tail(rid)
        return 200, {"id": rid, "state": rec.get("state"), "lines": lines, "stages": stages(rec, lines)}
    return 404, {"ok": False, "msg": "not found"}


def _who(ctx):
    """Who is asking, as tailscale serve says it: passed to Zeus, who has Janus check it. Never judged here."""
    h = getattr(ctx, "headers", None) or {}
    user = (h.get("Tailscale-User-Login") or "").strip()
    ip = (h.get("X-Forwarded-For") or "").split(",")[0].strip()
    return (["--user", user] if user else []) + (["--ip", ip] if ip else [])


def run(args):
    """Run zeus with fixed arguments (a list, never a shell). Returns (exit code, what Zeus said)."""
    env = {k: v for k, v in os.environ.items() if k not in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")}   # this is the Throne, never ssh
    try:
        p = subprocess.run([zeus_bin()] + args, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL, env=env)
    except FileNotFoundError:
        return 127, "The zeus command is not installed on this machine (bash scripts/install.sh in the zeus repository)."
    except subprocess.TimeoutExpired:
        return 124, "Zeus did not answer within two minutes. Look at the Throne again in a moment; nothing is retried."
    said = "\n".join(x.strip() for x in (p.stdout, p.stderr) if x and x.strip())
    return p.returncode, said[-800:]


def post(path, body, ctx):
    rid, sha, repo = str(body.get("id", "")), str(body.get("sha", "")), str(body.get("repo", ""))
    if path == "approve":
        if not (ID.match(rid) and SHA.match(sha)):
            return 400, {"ok": False, "msg": "bad id or commit"}
        args = ["approve", rid, "--sha", sha] + _who(ctx)
    elif path == "decline":
        reason = str(body.get("reason", ""))
        if not (ID.match(rid) and WORD.match(reason)):
            return 400, {"ok": False, "msg": "bad id or reason word"}
        args = ["decline", rid, "--reason", reason] + _who(ctx)
    elif path == "rollback":
        if not (REPO.match(repo) and SHA.match(sha)):
            return 400, {"ok": False, "msg": "bad repository or commit"}
        args = ["rollback", repo, "--sha", sha] + _who(ctx)
    elif path == "recheck":
        if not REPO.match(repo):
            return 400, {"ok": False, "msg": "bad repository"}
        args = ["request", repo]
    elif path == "seen":
        args = ["seen"]
    else:
        return 404, {"ok": False, "msg": "not found"}
    code, said = run(args)
    return 200, {"ok": code == 0, "code": code, "msg": said}
