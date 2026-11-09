"""The decision inbox (docs/design.md §14, tier 3; 0.5.0): a session asks the owner a plain question with options, the
owner answers with one tap on the Throne (or `zeus answer` in a shell), and the session reads the answer back with
`zeus asks --json`. An answer is data, never a command: Zeus runs nothing because of it, and nothing in an ask record
names a repository, a commit, a unit or a command.

Each ask is one record, queue/ask-<time>-<project>.json, signed like a log line (an HMAC `sig` over its canonical JSON,
ZEUS_LOG_KEY) and re-signed by Zeus at each change of state: open -> answered | expired. Every change also appends a
signed line to the approvals log (action ask, answer, ask-expired; tier 3), so `zeus verify-log` covers both. A record
is answered once: the first answer wins (an O_EXCL marker in queue/.answers/), a second one is refused, and a record
changed outside Zeus (its sig no longer matches) is refused rather than answered and left out of `zeus asks`. `via:
elaia` (a tap on the Throne) needs --user and --ip, which are refused in an ssh session, as `zeus approve` refuses them."""
import getpass
import json
import os
import re
import sys
from datetime import timedelta

from zeus import log, queue, version

KIND = "decision"
TIER = 3
OPEN, ANSWERED, EXPIRED = "open", "answered", "expired"
STATES = (OPEN, ANSWERED, EXPIRED)
PREFIX = "ask-"
PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ID = re.compile(r"^ask-\d{8}-\d{6}-[a-z0-9][a-z0-9-]{0,39}(-\d{1,3})?$")
EXPIRES = re.compile(r"^(\d{1,4})([mhd])$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
CONTROL_BUT_NEWLINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
TITLE_MAX, OPTION_MAX, CONTEXT_MAX = 200, 80, 2000
OPTIONS_MIN, OPTIONS_MAX = 2, 8
EXPIRES_MIN, EXPIRES_MAX = timedelta(minutes=10), timedelta(days=30)
DEFAULT_EXPIRES = "3d"


class AskError(Exception):
    """An ask that cannot be made or answered; the message says why in plain words. code is the exit status."""
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


# ---- the records -----------------------------------------------------------------------------------------------------
def folder(store):
    return store.queue


def path(store, aid):
    return folder(store) / (aid + ".json")


def answers(store):
    """queue/.answers/: one marker per answered ask, made with O_EXCL, so two answers at once can't both win."""
    return folder(store) / ".answers"


def duration(text):
    """'3d', '12h', '90m' -> timedelta; AskError when the form or the range is wrong."""
    m = EXPIRES.match(str(text or "").strip())
    if not m:
        raise AskError("--expires is a number and m, h or d (e.g. 3d, 12h, 90m), not %r." % text, 2)
    unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]
    d = timedelta(**{unit: int(m.group(1))})
    if not EXPIRES_MIN <= d <= EXPIRES_MAX:
        raise AskError("--expires must be between 10m and 30d, not %s." % text, 2)
    return d


def check_fields(project, title, options, context):
    """The ask's fields, cleaned; AskError (exit 2) with every problem when something is wrong."""
    bad = []
    project = str(project or "").strip()
    title = str(title or "").strip()
    context = str(context or "").strip()
    options = [str(o).strip() for o in (options or [])]
    if not PROJECT.match(project):
        bad.append("--project is the asking project's id: lowercase letters, digits and dashes, up to 40 (e.g. zeus)")
    if not title or len(title) > TITLE_MAX or CONTROL.search(title):
        bad.append("--title is one line of 1 to %d characters" % TITLE_MAX)
    if not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
        bad.append("give %d to %d --option values" % (OPTIONS_MIN, OPTIONS_MAX))
    for o in options:
        if not o or len(o) > OPTION_MAX or CONTROL.search(o):
            bad.append("each --option is one line of 1 to %d characters (%r is not)" % (OPTION_MAX, o[:OPTION_MAX + 5]))
    if len({o.casefold() for o in options}) != len(options):
        bad.append("the options must differ from each other")
    if len(context) > CONTEXT_MAX or CONTROL_BUT_NEWLINE.search(context):
        bad.append("--context is up to %d characters of text (new lines allowed)" % CONTEXT_MAX)
    if bad:
        raise AskError("zeus ask: " + "; ".join(bad) + ". Nothing was asked.", 2)
    return project, title, options, context


def who(user=None, ip=None, cmd="zeus answer"):
    """Who is asking or answering. `via: elaia` is the Throne's path: the plugin passes the tailnet login and the
    address, --user and --ip, both. In an ssh session the flags are refused, as `zeus approve` refuses them
    (approve.caller): a shell over ssh is recorded as `ssh` with its own user and address, never as a tap. A shell on
    the server itself without the flags is `local`. AskError (exit 2) when the flags can't be taken."""
    ssh = (os.environ.get("SSH_CONNECTION") or "").split()
    if ssh and (user or ip):
        raise AskError("%s: --user and --ip are refused in an ssh session: an answer over ssh is recorded as the "
                       "shell's user (via ssh), never as a tap on the Throne. Nothing was recorded." % cmd, 2)
    if user or ip:
        if not (user and ip):
            raise AskError("%s: --user and --ip go together (the Throne passes both). Nothing was recorded." % cmd, 2)
        return {"user": user, "via": "elaia", "ip": ip}
    if ssh:
        return {"user": getpass.getuser(), "via": "ssh", "ip": ssh[0]}
    return {"user": getpass.getuser(), "via": "local"}


def _key(cmd):
    """The log key, or AskError in plain words (no traceback) when secret.env is missing or loose."""
    try:
        return log.key()
    except log.LogError as e:
        raise AskError("%s: %s" % (cmd, e))


def new_id(store, project, t):
    base = "%s%s-%s" % (PREFIX, t.strftime("%Y%m%d-%H%M%S"), project)
    aid, n = base, 1
    while path(store, aid).exists():
        n += 1
        aid = "%s-%d" % (base, n)
    return aid


def write(store, rec):
    """Sign the record and write it atomically. Returns the signed record."""
    signed = log.sign(rec)
    folder(store).mkdir(parents=True, exist_ok=True)
    p = path(store, rec["id"])
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(signed, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    return signed


def read(store, aid):
    """The record, or None. Never trusts the file for its id: the id must have the ask form."""
    if not ID.match(aid or ""):
        return None
    try:
        rec = json.loads(path(store, aid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and rec.get("id") == aid else None


def all_(store):
    """Every ask record, newest first (the id starts with its time). Broken files are skipped, never fixed."""
    out = []
    try:
        paths = sorted(folder(store).glob(PREFIX + "*.json"), reverse=True)
    except OSError:
        return out
    for p in paths:
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("id") == p.stem and ID.match(p.stem):
            out.append(rec)
    return out


def _line(rec, action, now, **extra):
    entry = {"t": queue.stamp(now), "id": rec["id"], "action": action, "kind": KIND, "tier": TIER,
             "project": rec["project"], "title": rec["title"], "zeus": version()}
    entry.update(extra)
    return entry


# ---- the lifecycle ---------------------------------------------------------------------------------------------------
def ask(store, project, title, options, context="", expires=DEFAULT_EXPIRES, now=None, user=None, ip=None):
    """open: a new signed record and a signed `ask` line. Returns the record; AskError when it can't be made."""
    now = now or queue.now_local()
    project, title, options, context = check_fields(project, title, options, context)
    until = now + duration(expires)
    by = who(user, ip, "zeus ask")
    try:
        log.key()
    except log.LogError as e:
        raise AskError("zeus ask: %s Nothing was asked." % e)
    aid = new_id(store, project, now)
    rec = {"id": aid, "kind": KIND, "tier": TIER, "state": OPEN, "project": project, "title": title,
           "options": options, "context": context, "asked": queue.stamp(now), "expires": queue.stamp(until),
           "by": by, "answer": None, "zeus": version()}
    log.append(store, _line(rec, "ask", now, options=options, expires=rec["expires"], by=rec["by"]), when=now)
    return write(store, rec)


def expire(store, now=None, k=None):
    """Open asks past their expiry become expired (here and when answering; no timer). Returns those records. Only a
    record whose signature matches is touched: one changed outside Zeus stays as it is for `zeus verify-log`. AskError
    when the key is missing (never a traceback)."""
    now = now or queue.now_local()
    gone = []
    recs = all_(store)
    if not recs:
        return gone
    k = k or _key("zeus asks")
    for rec in recs:
        t = queue.parse_time(rec.get("expires"))
        if rec.get("state") == OPEN and t is not None and now >= t and log.check(rec, k):
            rec["state"] = EXPIRED
            log.append(store, _line(rec, "ask-expired", now, expires=rec["expires"]), when=now)
            gone.append(write(store, rec))
    return gone


def answer(store, aid, option=None, index=None, now=None, user=None, ip=None):
    """open -> answered, once. option is an option's exact text, or index its number from 1. Returns the record."""
    now = now or queue.now_local()
    if not ID.match(aid or ""):
        raise AskError("zeus answer: %r is not an ask id (ask-YYYYMMDD-HHMMSS-<project>)." % aid, 2)
    by = who(user, ip)
    rec = read(store, aid)
    if rec is None:
        raise AskError("zeus answer: no ask %s." % aid)
    try:
        if not log.check(rec):
            raise AskError("zeus answer: %s was changed outside Zeus (its signature no longer matches); it is not "
                           "answered. Run `zeus verify-log` and ask again." % aid)
    except log.LogError as e:
        raise AskError("zeus answer: %s Nothing was recorded." % e)
    if rec.get("state") == OPEN:
        t = queue.parse_time(rec.get("expires"))
        if t is not None and now >= t:
            expire(store, now)
            rec = read(store, aid)
    if rec.get("state") == ANSWERED:
        given = (rec.get("answer") or {}).get("option")
        raise AskError("zeus answer: %s was already answered (%r); an answer can't change after it is given." % (aid, given))
    if rec.get("state") != OPEN:
        raise AskError("zeus answer: %s is %s, not open; only an open ask can be answered." % (aid, rec.get("state")))
    options = rec.get("options") or []
    if index is not None:
        if not (isinstance(index, int) and 1 <= index <= len(options)):
            raise AskError("zeus answer: --index is a number from 1 to %d." % len(options), 2)
        i = index - 1
    elif option in options:
        i = options.index(option)
    else:
        raise AskError("zeus answer: %r is not one of the options: %s." % (option, " | ".join(options)), 2)
    answers(store).mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(answers(store) / (aid + ".json"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise AskError("zeus answer: %s was answered a moment ago; an answer can't change after it is given." % aid)
    given = {"option": options[i], "index": i + 1, "by": by, "at": queue.stamp(now)}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(given, ensure_ascii=False) + "\n")
    log.append(store, _line(rec, "answer", now, option=options[i], index=i + 1, by=by), when=now)
    rec["state"], rec["answer"] = ANSWERED, given
    return write(store, rec)


def listing(store, which="open", now=None, project=None, aid=None, bad=None):
    """The asks for `zeus asks`: expiry applied first, then open ones (or all), newest first. A record whose signature
    does not match (changed outside Zeus) is left out, so a session never reads a forged answer; its id goes into
    `bad` when a list is given. AskError when the key is missing."""
    if not all_(store):
        return []
    k = _key("zeus asks")
    expire(store, now, k)
    recs = []
    for r in all_(store):
        if log.check(r, k):
            recs.append(r)
        elif bad is not None:
            bad.append(r["id"])
    if aid:
        recs = [r for r in recs if r["id"] == aid]
    if project:
        recs = [r for r in recs if r.get("project") == project]
    return recs if which == "all" or aid else [r for r in recs if r.get("state") == OPEN]


def line(rec):
    """One line for the terminal."""
    head = "%s: %s (%s) %s" % (rec["id"], rec.get("state"), rec.get("project"), json.dumps(rec.get("title"), ensure_ascii=False))
    if rec.get("state") == ANSWERED:
        a = rec.get("answer") or {}
        by = a.get("by") or {}
        return "%s -> %s by %s (%s) at %s" % (head, json.dumps(a.get("option"), ensure_ascii=False), by.get("user"),
                                              by.get("via"), a.get("at"))
    opts = " | ".join(rec.get("options") or [])
    return "%s [%s]; %s %s" % (head, opts, "expires" if rec.get("state") == OPEN else "expired", rec.get("expires"))


# ---- the command line ------------------------------------------------------------------------------------------------
def _fail(e):
    print(str(e), file=sys.stderr)
    return e.code


def main_ask(args):
    try:
        rec = ask(queue.Store(), args.project, args.title, args.option, args.context or "", args.expires)
    except AskError as e:
        return _fail(e)
    if args.json:
        print(json.dumps(rec, ensure_ascii=False, indent=1))
    else:
        print(rec["id"])
        print(line(rec))
    return 0


def main_answer(args):
    if (args.option is None) == (args.index is None):
        print("zeus answer: give the option's text or --index N (one of the two).", file=sys.stderr)
        return 2
    try:
        rec = answer(queue.Store(), args.id, option=args.option, index=args.index, user=args.user, ip=args.ip)
    except AskError as e:
        return _fail(e)
    print(line(rec))
    return 0


def main_asks(args):
    bad = []
    try:
        recs = listing(queue.Store(), "all" if args.all else "open", project=args.project, aid=args.id, bad=bad)
    except AskError as e:
        return _fail(e)
    if bad:
        print("zeus asks: left out %d record%s changed outside Zeus (the signature does not match): %s. Run `zeus "
              "verify-log`; nothing in them is an answer." % (len(bad), "" if len(bad) == 1 else "s", ", ".join(bad)),
              file=sys.stderr)
    if args.json:
        print(json.dumps(recs, ensure_ascii=False, indent=1))
        return 0
    if not recs:
        print("zeus asks: nothing %s" % ("asked yet" if args.all else "is waiting for an answer"))
    for r in recs:
        print(line(r))
    return 0
