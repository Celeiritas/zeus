"""zeus rollback <repo> (docs/design.md §6 "Roll back by hand"; issue #9): put the previous approved commit back. The
target is state.previous, a commit that was approved and ran before, so no fresh deploy approval is needed; but who is
asking still is: Janus verifies (action rollback), Zeus writes a signed line, and the same deploy-run does the work.
Rolling back twice goes one step further back each time: the history is a stack."""
import sys
from datetime import timedelta

from zeus import approve, config, deploy, log, queue, version

EXPIRES_MIN = 10           # a rollback record is for now: it expires quickly, and nothing re-shows it as waiting


def target(store, key):
    """(what runs now, what a rollback would put back) from state/<repo>.json; either may be None."""
    st = deploy.read_state(store, key)
    return st.get("deployed"), st.get("previous")


def ask(live, prev, read=None, write=None):
    write = write or (lambda t: (sys.stderr.write(t), sys.stderr.flush()))
    read = read or sys.stdin.readline
    write("Roll back %s %s (%s) → %s (%s)? type yes: " % (live.get("repo", ""), live.get("version") or "", live["sha"][:7],
                                                         prev.get("version") or "", prev["sha"][:7]))
    try:
        answer = read()
    except (OSError, EOFError, KeyboardInterrupt):
        answer = ""
    return (answer or "").strip() == "yes"


def rollback(store, cfg, name, who, sha=None, now=None, out=None, confirm=None):
    """Returns the exit status: 0 approved and the rollback started · 1 refused, cancelled or held."""
    now = now or queue.now_local()
    out = out or (lambda s: None)
    key = approve.resolve(cfg, name)
    if key is None:
        out("zeus rollback: %s is not one repository in repos.json." % name)
        return 1
    live, prev = target(store, key)
    if not live or not prev or not prev.get("sha"):
        out("zeus rollback: %s has no previous approved commit to go back to; nothing was done." % key)
        return 1
    if sha is not None and sha != prev["sha"]:
        out("zeus rollback: the target %s is not the one the page showed (%s); nothing was done." % (prev["sha"][:7], sha[:7]))
        return 1
    if who.via == "elaia" and sha is None:
        out("zeus rollback: --sha is required from the Throne: the commit the page showed as the target.")
        return 1
    if confirm is not None and not confirm(dict(live, repo=key), dict(prev, repo=key)):
        out("zeus rollback: cancelled; nothing was done.")
        return 1
    store.make_folders()
    rid = store.new_id(key.split("/")[-1] + "-rollback", prev["sha"][:7], now)
    rec = {"id": rid, "action": "rollback", "state": queue.PENDING, "repo": key, "sha": prev["sha"],
           "previous": live["sha"], "subject": "Roll back to %s" % (prev.get("version") or prev["sha"][:7]),
           "version": prev.get("version") or "", "from": {"sha": live["sha"], "version": live.get("version")},
           "checks": "none", "changelog": [], "files": None, "rules_changed": [], "seen": queue.stamp(now),
           "expires": queue.stamp(now + timedelta(minutes=EXPIRES_MIN)),
           "verified": {"on_main": None, "at": None}, "approval": None, "deploy": None}
    request = {"action": "rollback", "repo": key, "sha": prev["sha"], "user": who.user, "ip": who.ip,
               "when": queue.stamp(now)}
    janus = approve.Janus(request).run()
    rec["janus"] = {"verdict": janus.verdict, "reasons": janus.reasons, "at": queue.stamp(now), "user": who.user, "ip": who.ip}
    if janus.verdict != "allowed":
        rec["state"] = "refused" if janus.verdict == "refused" else "failed"
        rec["reason"] = ("Janus refused: " if janus.verdict == "refused" else "Janus could not verify: ") + "; ".join(janus.reasons)
        store.write(rec)
        if janus.verdict == "failed":
            approve.alert_write("%s: a rollback could not be verified by Janus (%s)" % (key.split("/")[-1], "; ".join(janus.reasons)), now)
        out("zeus rollback: " + rec["reason"])
        return 1
    by = {"user": who.user or janus.user, "ip": who.ip, "via": who.via}
    if janus.node:
        by["node"] = janus.node
    try:
        entry = log.append(store, {"t": queue.stamp(now), "id": rid, "action": "rollback", "repo": key, "sha": prev["sha"],
                                   "version": rec["version"], "from": rec["from"], "by": by, "janus": "allowed",
                                   "zeus": version()}, when=now)
    except log.LogError as e:
        out("zeus rollback: " + str(e))
        return 1
    rec["state"] = "approved"
    rec["approval"] = {"decision": "approved", "by": by, "at": entry["t"], "janus": "allowed", "sig": entry["sig"]}
    store.write(rec)
    ok, text = approve.start_deploy(rid)
    rec["deploy"] = {"started": ok, "unit" if ok else "error": text, "at": queue.stamp(queue.now_local())}
    store.write(rec)
    if not ok:
        approve.alert_write("%s: the rollback was approved but could not be started: %s" % (key.split("/")[-1], text), now)
        out("zeus rollback: approved and logged, but it did not start: %s" % text)
        return 1
    out("%s: rollback of %s %s → %s approved by %s; running as %s" % (rid, key, live.get("version") or live["sha"][:7],
                                                                      rec["version"] or prev["sha"][:7], by["user"], text))
    return 0


def main(args):
    who = approve.caller(args.user, args.ip)
    if who.error:
        print("zeus rollback: " + who.error, file=sys.stderr)
        return 1
    cfg = config.check(getattr(args, "file", None))
    if cfg.errors:
        print("zeus rollback: %s has errors; run `zeus check-config`." % config.short(cfg.path), file=sys.stderr)
        return 1
    return rollback(queue.Store(), cfg, args.repo, who, sha=args.sha, out=print,
                    confirm=ask if who.via == "ssh" else None)
