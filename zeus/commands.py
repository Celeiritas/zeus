"""zeus request, decline and status (docs/design.md §3, §4; issue #4). request makes a record for the current main of
an allowlisted repository without waiting for Hermes; decline closes a waiting one with a signed line and no Janus
call (declining changes nothing); status is what the Throne page and `zeus status --json` read."""
import getpass
import json
import os
import re
import sys

from zeus import config, log, queue, version

REASON = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
CHANGELOG_LINES = 10


def _blob(fetch, sha, name):
    code, out, err = queue.git(["show", "%s:%s" % (sha, name)], cwd=fetch.where)
    return out if code == 0 else None


def changelog_lines(text):
    """The bullet lines of the first section of a CHANGELOG.md, at most CHANGELOG_LINES."""
    lines, inside = [], False
    for line in (text or "").splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = True
        elif inside and line.startswith("- "):
            lines.append(line.rstrip())
            if len(lines) >= CHANGELOG_LINES:
                break
    return lines


def request(store, cfg, repo_key, now=None, out=None):
    """A record for the current main of an allowlisted repository, through the same admission as a Hermes message.
    Returns the record, or None when nothing was fetched (unknown repository, broken config)."""
    now = now or queue.now_local()
    out = out or (lambda s: None)
    if cfg is None or repo_key not in cfg.repos:
        out("zeus request: %s is not in repos.json; nothing was fetched." % repo_key)
        return None
    repo = cfg.repos[repo_key]
    store.make_folders()
    fetch = queue.Fetch(store, repo)
    if not fetch.run():
        out("zeus request: " + fetch.error)
        return None
    code, sha, err = queue.git(["rev-parse", "refs/remotes/origin/" + config.BRANCH], cwd=fetch.where)
    sha = sha.strip()
    if code != 0 or not queue.SHA.match(sha):
        out("zeus request: cannot read %s of %s after the fetch: %s" % (config.BRANCH, repo_key, err.strip()))
        return None
    deployed = store.deployed(repo_key) or {}
    previous = ((deployed.get("deployed") or {}).get("sha")) if isinstance(deployed.get("deployed"), dict) else None
    code, subject, err = queue.git(["log", "-1", "--format=%s", sha], cwd=fetch.where)
    ver = _blob(fetch, sha, "VERSION")
    msg = {"type": queue.MESSAGE_TYPE, "repo": repo_key, "sha": sha,
           "previous": previous if previous and queue.SHA.match(previous) else None,
           "subject": subject.strip() if code == 0 else "", "version": (ver or "").strip(), "checks": "none",
           "changelog": changelog_lines(_blob(fetch, sha, "CHANGELOG.md")), "seen": queue.stamp(now)}
    rec = queue.admit(store, cfg, msg, now)
    out(queue.line(rec))
    return rec


def decline(store, rid, reason, user=None, ip=None, now=None, out=None):
    """pending → declined with a signed line in the approvals log and no Janus call. Returns the exit status."""
    now = now or queue.now_local()
    out = out or (lambda s: None)
    if not REASON.match(reason or ""):
        out("zeus decline: the reason is one word of lowercase letters, digits and dashes (e.g. not-now), not %r." % reason)
        return 2
    queue.expire(store, now)
    rec = store.read(rid)
    if rec is None:
        out("zeus decline: no request %s." % rid)
        return 1
    if rec["state"] != queue.PENDING:
        out("zeus decline: %s is %s, not pending; only a waiting request can be declined." % (rid, rec["state"]))
        return 1
    by = {"user": user or getpass.getuser(), "via": "elaia" if user else "ssh"}
    if ip:
        by["ip"] = ip
    try:
        entry = log.append(store, {"t": queue.stamp(now), "id": rid, "action": "decline", "repo": rec["repo"],
                                   "sha": rec["sha"], "version": rec["version"], "by": by, "reason": reason,
                                   "janus": "not asked", "zeus": version()}, when=now)
    except log.LogError as e:
        out("zeus decline: " + str(e))
        return 1
    rec["state"] = "declined"
    rec["reason"] = reason
    rec["approval"] = {"decision": "declined", "by": by, "at": entry["t"], "reason": reason}
    store.write(rec)
    out(queue.line(rec))
    return 0


def status(store, cfg, now=None):
    """The status document (docs/contract.md): deployed per repository, the queue, what is deploying, history."""
    now = now or queue.now_local()
    pending = queue.listing(store, now)
    history = store.history()
    deployed = {}
    for key in (cfg.repos if cfg else {}):
        st = store.deployed(key)
        deployed[key] = (st or {}).get("deployed") if isinstance(st, dict) else None
    deploying = [r for r in history if r.get("state") in ("approved", "deploying")]
    return {"zeus": version(), "at": queue.stamp(now),
            "deployed": deployed,
            "queue": pending,
            "deploying": deploying,
            "history": [r for r in history if r.get("state") not in ("approved", "deploying")][:10],
            "line": status_line(deployed, pending)}


def status_line(deployed, pending):
    """One sentence for the Home page: what waits and what is live."""
    n = len(pending)
    parts = ["%d deploy%s waiting" % (n, "" if n == 1 else "s") if n else "nothing waiting"]
    for key, d in deployed.items():
        if d:
            parts.append("%s %s live" % (key.split("/")[-1], d.get("version") or (d.get("sha") or "")[:7]))
    return " · ".join(parts)


def text(doc):
    """status for the terminal."""
    lines = [doc["line"]]
    for r in doc["queue"]:
        lines.append("  waiting   " + queue.line(r))
    for r in doc["deploying"]:
        lines.append("  deploying " + queue.line(r))
    for key, d in doc["deployed"].items():
        lines.append("  live      %s: %s" % (key, "%s (%s) since %s" % (d.get("version"), (d.get("sha") or "")[:7], d.get("at"))
                                              if d else "nothing deployed by Zeus yet"))
    for r in doc["history"]:
        lines.append("  history   " + queue.line(r))
    return "\n".join(lines)


# ---- the command line ------------------------------------------------------------------------------------------------
def _config(args, name):
    cfg = config.check(getattr(args, "file", None))
    if cfg.errors:
        print("zeus %s: %s has %d error%s; run `zeus check-config`." % (name, config.short(cfg.path), len(cfg.errors),
                                                                        "" if len(cfg.errors) == 1 else "s"), file=sys.stderr)
        return None
    return cfg


def main_request(args):
    cfg = _config(args, "request")
    if cfg is None:
        return 1
    rec = request(queue.Store(), cfg, args.repo, out=print)
    return 0 if rec is not None and rec["state"] == queue.PENDING else 1


def main_decline(args):
    return decline(queue.Store(), args.id, args.reason, user=args.user, ip=args.ip, out=print)


def main_status(args):
    cfg = _config(args, "status")
    doc = status(queue.Store(), cfg)
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=1))
    else:
        print(text(doc))
    return 0 if cfg is not None else 1
