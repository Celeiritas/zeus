"""The queue (docs/design.md §3): turn Hermes's deploy-request messages in requests/ into records in queue/, and keep
their states. Every file leaves requests/ whatever happens, or zeus-queue.path would fire forever. A message is only a
hint: Zeus fetches main of the allowlisted url himself and checks the commit is on it before the record can be shown
as approvable. Nothing here runs a command from a message; git runs with hooks off and prompts off."""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

from zeus import config, init

MESSAGE_TYPE = "deploy-request"
SHA = re.compile(r"^[0-9a-f]{40}$")
PENDING = "pending"
WAITING = ("pending", "held")      # held: approved by the owner, Janus failed to answer; approvable again
# Terminal states live in queue/.done/ (never deleted); only pending records sit in queue/ itself.
DONE = ("refused", "superseded", "expired", "declined", "deployed", "failed", "rolled-back")
MESSAGE_MAX = 64 * 1024
GIT_TIMEOUT = 120


def now_local():
    return datetime.now().astimezone()


def stamp(t):
    return t.isoformat(timespec="seconds")


def parse_time(text):
    """An ISO time with offset, or None. A time without offset is taken as local."""
    try:
        t = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.astimezone()


# ---- the store -------------------------------------------------------------------------------------------------------
class Store:
    """queue/<id>.json (pending) and queue/.done/<id>.json (terminal) under the data folder."""
    def __init__(self, data=None):
        self.data = Path(data) if data else init.data_path()
        self.requests = self.data / "requests"
        self.queue = self.data / "queue"
        self.done = self.queue / ".done"
        self.mirrors = self.data / "mirrors"

    def make_folders(self):
        for p in (self.requests, self.queue, self.done, self.mirrors):
            p.mkdir(parents=True, exist_ok=True)

    def write(self, rec):
        """Write a record where its state says it lives, atomically, and remove it from the other folder."""
        here, there = (self.done, self.queue) if rec["state"] in DONE else (self.queue, self.done)
        tmp = here / (rec["id"] + ".json.tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, here / (rec["id"] + ".json"))
        try:
            (there / (rec["id"] + ".json")).unlink()
        except FileNotFoundError:
            pass
        return rec

    def read(self, id):
        for folder in (self.queue, self.done):
            p = folder / (id + ".json")
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        return None

    def pending(self, states=("pending",)):
        """The records in queue/ in those states, newest first (by id, which starts with the time). WAITING adds held
        ones (Janus could not answer): they still expire, still count as a duplicate and are still superseded."""
        recs = []
        for p in sorted(self.queue.glob("*.json"), reverse=True):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            if rec.get("state") in states:
                recs.append(rec)
        return recs

    def history(self):
        """Everything in .done/, newest first."""
        recs = []
        for p in sorted(self.done.glob("*.json"), reverse=True):
            try:
                recs.append(json.loads(p.read_text(encoding="utf-8")))
            except ValueError:
                continue
        return recs

    def deployed(self, repo_key):
        """state/<repo>.json (written by the deploy, 0.3), or None before the first deploy."""
        p = self.data / "state" / (repo_key.replace("/", "--") + ".json")
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def new_id(self, name, sha7, t):
        base = "%s-%s-%s" % (t.strftime("%Y%m%d-%H%M%S"), name, sha7)
        id, n = base, 1
        while (self.queue / (id + ".json")).exists() or (self.done / (id + ".json")).exists():
            n += 1
            id = "%s-%d" % (base, n)
        return id


# ---- git, the one place it is called -------------------------------------------------------------------------------
def git(args, cwd=None, env=None, timeout=GIT_TIMEOUT):
    """Run git with hooks and prompts off. Returns (exit code, stdout, stderr). Tests replace this to count fetches."""
    full = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    full.update(env or {})
    try:
        r = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=cwd, env=full, capture_output=True,
                           text=True, timeout=timeout)
    except OSError as e:
        return 127, "", "git: %s" % e
    except subprocess.TimeoutExpired:
        return 124, "", "git %s: no answer in %d s" % (args[0], timeout)
    return r.returncode, r.stdout, r.stderr


def git_env(repo):
    """The environment for a fetch: the repo's own read-only key for a private repository, nothing else."""
    if repo.private and repo.deploy_key:
        return {"GIT_SSH_COMMAND": "ssh -i %s -o IdentitiesOnly=yes -o BatchMode=yes" % repo.deploy_key}
    return {}


class Fetch:
    """One fetch of main of an allowlisted repository, into its clone if there is one, else into Zeus's bare mirror."""
    def __init__(self, store, repo):
        self.store, self.repo = store, repo
        self.where = None
        self.error = None

    def place(self):
        """The clone at path when it is a git repository already, else the mirror (made on first use)."""
        path = self.repo.path
        if path and (path / ".git").exists():
            return path
        mirror = self.store.mirrors / (self.repo.key + ".git")
        if not (mirror / "HEAD").exists():
            mirror.parent.mkdir(parents=True, exist_ok=True)
            code, out, err = git(["init", "-q", "--bare", str(mirror)])
            if code != 0:
                raise RuntimeError("cannot make the mirror %s: %s" % (config.short(mirror), err.strip()))
        return mirror

    def run(self):
        """git fetch <url> main into refs/remotes/origin/main. The url is the allowlist's, never the clone's or the
        message's. Returns True when main is fetched."""
        try:
            self.where = self.place()
        except RuntimeError as e:
            self.error = str(e)
            return False
        code, out, err = git(["fetch", "--quiet", "--no-tags", self.repo.url,
                              "+refs/heads/%s:refs/remotes/origin/%s" % (config.BRANCH, config.BRANCH)],
                             cwd=self.where, env=git_env(self.repo))
        if code != 0:
            self.error = "could not fetch %s of %s: %s" % (config.BRANCH, self.repo.url, (err.strip() or "git exit %d" % code))
            return False
        return True

    def on_main(self, sha):
        """Zeus's own check: is the commit an ancestor of (or equal to) origin/main?"""
        code, out, err = git(["merge-base", "--is-ancestor", sha, "origin/" + config.BRANCH], cwd=self.where)
        return code == 0

    def changed(self, previous, sha):
        """(files, paths) changed between previous and sha; previous..sha when previous is known, else the commit's own
        change. None when git cannot say."""
        base = previous if previous and git(["cat-file", "-e", previous + "^{commit}"], cwd=self.where)[0] == 0 else sha + "^"
        code, out, err = git(["diff", "--name-only", base, sha], cwd=self.where)
        if code != 0:
            code, out, err = git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha], cwd=self.where)
            if code != 0:
                return None, []
        paths = [p for p in out.splitlines() if p.strip()]
        return len(paths), paths


# ---- messages to records -------------------------------------------------------------------------------------------
def read_message(path):
    """The message as a dict, or a sentence saying why it cannot be used."""
    try:
        if path.stat().st_size > MESSAGE_MAX:
            return None, "the file is larger than %d KB; a deploy-request is a few hundred bytes" % (MESSAGE_MAX // 1024)
        doc = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        return None, "cannot read it (%s)" % e.strerror
    except ValueError as e:
        return None, "not valid JSON (%s)" % e
    if not isinstance(doc, dict):
        return None, "not a JSON object"
    if doc.get("type") != MESSAGE_TYPE:
        return None, "not a %s message (type is %r)" % (MESSAGE_TYPE, doc.get("type"))
    return doc, None


def field_errors(msg):
    """What is wrong with the message's fields, in plain words (empty when nothing)."""
    bad = []
    if not isinstance(msg.get("repo"), str) or not config.REPO_KEY.match(msg.get("repo") or ""):
        bad.append("repo must be owner/name")
    if not isinstance(msg.get("sha"), str) or not SHA.match(msg["sha"]):
        bad.append("sha must be 40 hex characters")
    if msg.get("previous") is not None and (not isinstance(msg["previous"], str) or not SHA.match(msg["previous"])):
        bad.append("previous must be 40 hex characters or null")
    for k in ("subject", "version", "checks"):
        if msg.get(k) is not None and not isinstance(msg[k], str):
            bad.append("%s must be text" % k)
    if msg.get("changelog") is not None and not (isinstance(msg["changelog"], list)
                                                 and all(isinstance(l, str) for l in msg["changelog"])):
        bad.append("changelog must be a list of lines")
    return bad


def process(store, cfg, now=None, out=None):
    """One run of the queue: every file in requests/ becomes a record (pending, or refused with the reason) and leaves
    requests/. cfg may be None when repos.json is broken: then every request is refused, so the folder still empties.
    Returns the records written, in order."""
    now = now or now_local()
    out = out or (lambda s: None)
    store.make_folders()
    written = []
    for path in sorted(store.requests.iterdir()):
        if not path.is_file():
            continue
        rec = one(store, cfg, path, now)
        written.append(rec)
        try:
            path.unlink()
        except OSError as e:
            out("warning: %s stays in requests/ (%s); zeus-queue.path will fire again" % (path.name, e.strerror))
        out(line(rec))
    expire(store, now, out)
    return written


def one(store, cfg, path, now):
    """One request file to one record. Refusals are records too, so the History shows what arrived and why it stopped."""
    msg, why = read_message(path)
    name = re.sub(r"[^A-Za-z0-9._-]", "", path.stem.split("-")[0]) or "request"
    if msg is None:
        return store.write(_record(store.new_id(name, "nosha", now), None, now, cfg, state="refused",
                                   reason="%s: %s" % (path.name, why)))
    return admit(store, cfg, msg, now, name)


def admit(store, cfg, msg, now, name="request"):
    """One message (from a file, or made by `zeus request`) to one record: field check, allowlist, duplicate, fetch,
    on-main, diff, supersede. Every outcome is a record."""
    bad = field_errors(msg)
    sha = msg.get("sha") if isinstance(msg.get("sha"), str) else ""
    repo_key = msg.get("repo") if isinstance(msg.get("repo"), str) else "?"
    name = repo_key.split("/")[-1] or name
    rid = store.new_id(name, (sha[:7] if SHA.match(sha) else "nosha"), now)
    if bad:
        return store.write(_record(rid, msg, now, cfg, state="refused", reason="; ".join(bad)))
    if cfg is None:
        return store.write(_record(rid, msg, now, cfg, state="refused",
                                   reason="repos.json has errors; run `zeus check-config`. Nothing was fetched."))
    repo = cfg.repos.get(repo_key)
    if repo is None:
        return store.write(_record(rid, msg, now, cfg, state="refused",
                                   reason="%s is not in repos.json; nothing was fetched" % repo_key))
    for older in store.pending(WAITING):
        if older["repo"] == repo_key and older["sha"] == sha:
            return store.write(_record(rid, msg, now, cfg, state="refused",
                                       reason="already waiting as %s; nothing was fetched" % older["id"]))
    fetch = Fetch(store, repo)
    if not fetch.run():
        return store.write(_record(rid, msg, now, cfg, state="refused", reason=fetch.error))
    rec = _record(rid, msg, now, cfg)
    rec["verified"] = {"on_main": fetch.on_main(sha), "at": stamp(now_local())}
    if not rec["verified"]["on_main"]:
        rec["state"], rec["reason"] = "refused", "commit %s is not on %s of %s" % (sha[:7], config.BRANCH, repo_key)
        return store.write(rec)
    files, paths = fetch.changed(msg.get("previous"), sha)
    rec["files"] = files
    rec["rules_changed"] = [p for p in paths if p in repo.rules]
    for older in store.pending(WAITING):
        if older["repo"] == repo_key and older["id"] != rid:
            older["state"] = "superseded"
            older["superseded_by"] = {"id": rid, "version": rec["version"], "sha": sha}
            older["reason"] = "replaced by %s" % (rec["version"] or sha[:7])
            store.write(older)
    return store.write(rec)


def _record(rid, msg, now, cfg, state=PENDING, reason=None):
    msg = msg or {}
    seen = parse_time(msg.get("seen")) or now
    hours = cfg.expire_hours if cfg else config.DEFAULTS["expire_hours"]
    rec = {"id": rid, "action": "deploy", "state": state,
           "repo": msg.get("repo"), "sha": msg.get("sha"), "previous": msg.get("previous"),
           "subject": msg.get("subject") or "", "version": msg.get("version") or "", "checks": msg.get("checks") or "none",
           "changelog": list(msg.get("changelog") or []), "files": None, "rules_changed": [],
           "seen": stamp(seen), "expires": stamp(seen + timedelta(hours=hours)),
           "verified": {"on_main": False, "at": None}, "approval": None, "deploy": None}
    if reason:
        rec["reason"] = reason
    return rec


def expire(store, now=None, out=None):
    """Pending records past their expiry become expired (enforced here and at approval time; no timer)."""
    now = now or now_local()
    gone = []
    for rec in store.pending(WAITING):
        t = parse_time(rec.get("expires"))
        if t is not None and now >= t:
            rec["state"] = "expired"
            rec["reason"] = "expired %s" % rec["expires"]
            store.write(rec)
            gone.append(rec)
            if out:
                out(line(rec))
    return gone


def listing(store, now=None):
    """The queue as the page and `zeus status` see it: expiry applied first, then pending newest first."""
    expire(store, now)
    return store.pending()


def line(rec):
    """One line for the terminal: id, state, and the reason or the summary."""
    what = "%s %s" % (rec.get("repo") or "?", rec.get("version") or (rec.get("sha") or "")[:7])
    if rec["state"] == PENDING:
        files = rec.get("files")
        extra = ", %d file%s" % (files, "" if files == 1 else "s") if isinstance(files, int) else ""
        rules = " (changes the realm's own rules)" if rec.get("rules_changed") else ""
        return "%s: pending (%s%s)%s; expires %s" % (rec["id"], what, extra, rules, rec["expires"])
    return "%s: %s (%s): %s" % (rec["id"], rec["state"], what, rec.get("reason") or "")


def main(args):
    """zeus queue: process requests/ with repos.json; a broken repos.json refuses every request rather than leaving it."""
    cfg = config.check(args.file)
    if cfg.errors:
        for e in cfg.errors:
            print("error: " + e, file=sys.stderr)
        print("zeus queue: repos.json has %d error%s; every waiting request is refused until it is fixed."
              % (len(cfg.errors), "" if len(cfg.errors) == 1 else "s"), file=sys.stderr)
        cfg = None
    store = Store()
    recs = process(store, cfg, out=print)
    if not recs:
        print("zeus queue: nothing waiting")
    return 0 if cfg is not None else 1
