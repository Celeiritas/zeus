"""zeus approve (docs/design.md §4; issue #5): one approval, for one commit, once. Zeus checks the record is pending
and fresh, that the sha is the one the page showed, and fetches main again to see the commit is still on it. Then
Janus says who is asking (`janus verify-approval`: exit 0 allowed · 10 refused · 2 failed); Zeus writes the signed line
and starts the deploy detached under systemd-run, because the deploy may restart the very service that called him.
Nothing here is taken from the message or the page on trust: the sha is re-checked on origin/main first."""
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from zeus import config, log, queue, version

JANUS_TIMEOUT = 30
HELD = "held"                      # Janus could not answer; stays in queue/, approvable again, Argus glares
ALERT = "~/.local/state/argus-ui/alerts/zeus.json"      # or $ZEUS_ALERT
DEPLOY_LIMITS = ("-p", "CPUQuota=50%", "-p", "MemoryMax=1G", "-p", "IOWeight=50")


def command(env_name, default):
    """A program named by the environment (tests point ZEUS_JANUS / ZEUS_SYSTEMD_RUN at fakes): a JSON list when the
    value starts with [, else one word. Never from config, never from a message."""
    val = os.environ.get(env_name, "").strip()
    if not val:
        return [default]
    if val.startswith("["):
        try:
            cmd = json.loads(val)
            if isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd):
                return cmd
        except ValueError:
            pass
        return [default]
    return shlex.split(val, posix=os.name != "nt")


def alert_path():
    return Path(os.path.expanduser(os.environ.get("ZEUS_ALERT") or ALERT))


def alert_write(message, now):
    p = alert_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"message": message, "link": "throne", "at": queue.stamp(now)}, ensure_ascii=False) + "\n",
                 encoding="utf-8")


def alert_clear():
    try:
        alert_path().unlink()
    except FileNotFoundError:
        pass


class Janus:
    """One `janus verify-approval` call: the verdict word, the reasons, and the node name when Janus gives one."""
    def __init__(self, request):
        self.request = request
        self.verdict, self.reasons, self.node, self.raw = "failed", [], None, ""

    def run(self):
        cmd = command("ZEUS_JANUS", "janus") + ["verify-approval"]
        try:
            r = subprocess.run(cmd, input=json.dumps(self.request), capture_output=True, text=True, timeout=JANUS_TIMEOUT)
        except OSError as e:
            self.reasons = ["Janus could not be run: %s" % e]
            return self
        except subprocess.TimeoutExpired:
            self.reasons = ["Janus gave no answer in %d s" % JANUS_TIMEOUT]
            return self
        self.raw = r.stdout
        doc = None
        try:
            doc = json.loads(r.stdout)
        except ValueError:
            pass
        if isinstance(doc, dict):
            reasons = doc.get("reasons")
            self.reasons = [str(x) for x in reasons] if isinstance(reasons, list) else []
            self.node = doc.get("node") if isinstance(doc.get("node"), str) else None
        else:
            self.reasons = [l.strip() for l in (r.stdout + "\n" + r.stderr).splitlines() if l.strip()]
        if r.returncode == 0:
            self.verdict = "allowed"
        elif r.returncode == 10:
            self.verdict = "refused"
            if not self.reasons:
                self.reasons = ["Janus refused without a reason"]
        else:
            self.verdict = "failed"
            if not self.reasons:
                self.reasons = ["Janus failed (exit %d)" % r.returncode]
        return self


def zeus_bin():
    """The zeus to run under systemd-run: $ZEUS_BIN, else the file running now when it is a file, else `zeus`."""
    bin = os.environ.get("ZEUS_BIN")
    if bin:
        return bin
    me = sys.argv[0] if sys.argv else ""
    return me if me and os.path.isfile(me) and os.access(me, os.X_OK) else "zeus"


def start_deploy(rid):
    """systemd-run --user --unit=zeus-deploy-<id> ... zeus deploy-run <id>. Returns (ok, text)."""
    cmd = command("ZEUS_SYSTEMD_RUN", "systemd-run") + ["--user", "--unit=zeus-deploy-" + rid, "--collect",
                                                         *DEPLOY_LIMITS, zeus_bin(), "deploy-run", rid]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except OSError as e:
        return False, "systemd-run could not be run: %s" % e
    except subprocess.TimeoutExpired:
        return False, "systemd-run gave no answer in 60 s"
    if r.returncode != 0:
        return False, (r.stderr.strip() or r.stdout.strip() or "systemd-run exit %d" % r.returncode)
    return True, "zeus-deploy-" + rid


def approve(store, cfg, rid, sha, user, ip, now=None, via="elaia", out=None):
    """The approval. Returns the exit status: 0 approved and the deploy started · 1 refused or held (the reason was
    printed and, for Janus's verdicts, kept on the record)."""
    now = now or queue.now_local()
    out = out or (lambda s: None)
    queue.expire(store, now)
    rec = store.read(rid)
    if rec is None:
        out("zeus approve: no request %s." % rid)
        return 1
    if rec["state"] not in (queue.PENDING, HELD):
        out("zeus approve: %s is %s, not pending; an approval is for one commit, once." % (rid, rec["state"]))
        return 1
    if not isinstance(sha, str) or sha != rec["sha"]:
        out("zeus approve: the sha %s is not the one the page showed for %s (%s); nothing was approved."
            % ((sha or "")[:7] or "?", rid, rec["sha"][:7]))
        return 1
    if cfg is None or rec["repo"] not in cfg.repos:
        out("zeus approve: %s is not in repos.json; nothing was fetched." % rec["repo"])
        return 1
    fetch = queue.Fetch(store, cfg.repos[rec["repo"]])
    if not fetch.run():
        out("zeus approve: " + fetch.error)
        return 1
    rec["verified"] = {"on_main": fetch.on_main(sha), "at": queue.stamp(queue.now_local())}
    if not rec["verified"]["on_main"]:
        rec["state"], rec["reason"] = "refused", ("commit %s is no longer on %s of %s; a newer main needs its own "
                                                  "request" % (sha[:7], config.BRANCH, rec["repo"]))
        store.write(rec)
        out("zeus approve: " + rec["reason"])
        return 1
    request = {"action": "deploy", "repo": rec["repo"], "sha": sha, "user": user, "ip": ip, "when": queue.stamp(now)}
    janus = Janus(request).run()
    rec["janus"] = {"verdict": janus.verdict, "reasons": janus.reasons, "at": queue.stamp(now), "user": user, "ip": ip}
    if janus.verdict == "refused":
        if rec["state"] == HELD:
            rec["state"] = queue.PENDING
        store.write(rec)
        out("zeus approve: Janus refused: " + "; ".join(janus.reasons))
        return 1
    if janus.verdict == "failed":
        rec["state"] = HELD
        rec["reason"] = "held: " + "; ".join(janus.reasons)
        store.write(rec)
        alert_write("%s %s: Janus could not verify the approval (%s); the request is held"
                    % (rec["repo"].split("/")[-1], rec["version"] or sha[:7], "; ".join(janus.reasons)), now)
        out("zeus approve: held, Janus failed: " + "; ".join(janus.reasons))
        return 1
    by = {"user": user, "ip": ip, "via": via}
    if janus.node:
        by["node"] = janus.node
    try:
        entry = log.append(store, {"t": queue.stamp(now), "id": rid, "action": "deploy", "repo": rec["repo"], "sha": sha,
                                   "version": rec["version"], "by": by, "janus": "allowed", "zeus": version()}, when=now)
    except log.LogError as e:
        out("zeus approve: " + str(e))
        return 1
    rec["state"] = "approved"
    rec.pop("reason", None)
    rec["approval"] = {"decision": "approved", "by": by, "at": entry["t"], "janus": "allowed", "sig": entry["sig"]}
    store.write(rec)
    alert_clear()
    ok, text = start_deploy(rid)
    if not ok:
        rec["deploy"] = {"started": False, "error": text, "at": queue.stamp(queue.now_local())}
        store.write(rec)
        alert_write("%s %s was approved but the deploy could not be started: %s"
                    % (rec["repo"].split("/")[-1], rec["version"] or sha[:7], text), now)
        out("zeus approve: approved and logged, but the deploy did not start: %s" % text)
        return 1
    rec["deploy"] = {"started": True, "unit": text, "at": queue.stamp(queue.now_local())}
    store.write(rec)
    out("%s: approved (%s %s) by %s; deploying as %s" % (rid, rec["repo"], rec["version"] or sha[:7], user, text))
    return 0


def main(args):
    cfg = config.check(args.file)
    if cfg.errors:
        print("zeus approve: %s has %d error%s; run `zeus check-config`." % (config.short(cfg.path), len(cfg.errors),
                                                                             "" if len(cfg.errors) == 1 else "s"), file=sys.stderr)
        cfg = None
    return approve(queue.Store(), cfg, args.id, args.sha, args.user, args.ip, out=print)
