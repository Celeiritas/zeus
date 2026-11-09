"""Staged packs (docs/design.md §15): a repository whose build needs files only the PC has. The session builds on the
PC, stages an immutable pack on the server, and asks with `zeus stage-request <repo> --version V --sha S`. The request
carries {repo, version, sha} and nothing Zeus acts on beyond that: no path, no file name, no command. Zeus builds the
pack's name himself (<staging>/<version>-<sha>.tgz, the folder from repos.json), checks the pack against its .sha256,
the tar's contents and VERSION, the commit on main, and records the pack's size, modification time and sha256. At the
hold and again before the install he checks all of it once more (verify); deploy-run installs from his own copy."""
import hashlib
import os
import re
import stat
import sys
import tarfile
import time
import zlib
from datetime import timedelta

from zeus import commands, config, queue

VERSION = re.compile(r"^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}(-[0-9a-z]{1,16})?$")
SUM = re.compile(r"^([0-9a-fA-F]{64})(?:\s+\*?(\S+))?\s*$")
TESTS_MAX = 200
PACK_MAX = 2 * 1024 ** 3            # 2 GB packed
UNPACKED_MAX = 8 * 1024 ** 3        # 8 GB unpacked
MEMBERS_MAX = 100000
VERSION_MAX = 64                    # bytes of the pack's VERSION file
SUM_MAX = 4096                      # bytes of the .sha256 file
CHUNK = 1 << 20


class Refused(Exception):
    """The pack or the request can't be used; the message is the plain-sentence reason for the card."""


def bad_input(version, sha, tests=None):
    """What is wrong with the request's own fields, before anything is read (empty when nothing)."""
    bad = []
    if not isinstance(version, str) or not VERSION.match(version):
        bad.append("--version must look like 0.46.0 or 0.46.0-rc1 (digits and dots, no / or ..), not %r" % (version,))
    if not isinstance(sha, str) or not queue.SHA.match(sha):
        bad.append("--sha must be the full commit, 40 lowercase hex characters, not %r" % (sha,))
    if tests is not None and (not isinstance(tests, str) or len(tests) > TESTS_MAX or not tests.isprintable()):
        bad.append("--tests is one line of text, at most %d characters" % TESTS_MAX)
    return bad


def pack_name(version, sha):
    """<version>-<sha>.tgz, from checked parts only: the one way Zeus names a pack."""
    if bad_input(version, sha):
        raise Refused("version %r or commit %r is not one Zeus names a pack from" % (version, sha))
    return "%s-%s.tgz" % (version, sha)


def _plain(path):
    """A staged file: a regular file directly in the folder, not a link, Zeus's user's own, not writable by others."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise Refused("%s is not staged (no such file)" % config.short(path))
    except OSError as e:
        raise Refused("%s cannot be read (%s)" % (config.short(path), e.strerror))
    if stat.S_ISLNK(st.st_mode):
        raise Refused("%s is a symbolic link; Zeus reads only plain files in the staging folder" % config.short(path))
    if not stat.S_ISREG(st.st_mode):
        raise Refused("%s is not a regular file" % config.short(path))
    for why in config._owner_mode(st, config.short(path)):
        raise Refused("%s %s" % (config.short(path), why))
    return st


def locate(repo, version, sha):
    """(pack, its stat, .sha256 file): by the name Zeus builds, in the folder repos.json names. Never a request's path."""
    name = pack_name(version, sha)
    problems = config.staging_problems(repo.staging)
    if problems:
        raise Refused("the staging folder %s %s" % (config.short(repo.staging) if repo.staging else "", problems[0]))
    pack = repo.staging / name
    sumf = repo.staging / (name + ".sha256")
    if os.path.dirname(os.path.realpath(pack)) != os.path.realpath(repo.staging):
        raise Refused("%s is not directly in the staging folder" % name)
    st = _plain(pack)
    _plain(sumf)
    return pack, st, sumf


def read_sum(sumf, name):
    """The 64 hex of the .sha256 file (`<hex>` or sha256sum's `<hex>  <name>`), lower case."""
    try:
        with open(sumf, "rb") as f:
            text = f.read(SUM_MAX).decode("utf-8", "replace")
    except OSError as e:
        raise Refused("%s cannot be read (%s)" % (config.short(sumf), e.strerror))
    lines = [l for l in text.splitlines() if l.strip()]
    m = SUM.match(lines[0].strip()) if lines else None
    if not m:
        raise Refused("%s does not hold a sha256 (64 hex characters)" % config.short(sumf))
    if m.group(2) and m.group(2).lstrip("*") != name:
        raise Refused("%s names %s, not %s" % (config.short(sumf), m.group(2), name))
    return m.group(1).lower()


def digest(path, copy_to=None):
    """(sha256 hex, bytes) of a file; with copy_to, the same bytes go into a new file there (mode 600, never over one
    that exists), so what was hashed is exactly what was copied."""
    h, n = hashlib.sha256(), 0
    out = None
    if copy_to is not None:
        copy_to.parent.mkdir(parents=True, exist_ok=True)
        try:
            out = os.fdopen(os.open(copy_to, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600), "wb")
        except FileExistsError:
            raise Refused("Zeus's copy %s exists already; set it aside and approve again" % config.short(copy_to))
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(fd, "rb") as f:          # never through a link swapped in after the check
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                n += len(chunk)
                if n > PACK_MAX:
                    raise Refused("the pack is larger than %d GB" % (PACK_MAX // 1024 ** 3))
                h.update(chunk)
                if out:
                    out.write(chunk)
    except OSError as e:
        raise Refused("the pack cannot be read (%s)" % e.strerror)
    finally:
        if out:
            out.close()
    return h.hexdigest(), n


def _parts(name):
    return [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]


def inspect(path, version):
    """The tar rules of design §15.3 step 4 and the VERSION at its top. Returns (files, unpacked bytes)."""
    files, total, members, found, seen = 0, 0, 0, None, set()
    try:
        with tarfile.open(path, "r:gz") as tf:
            for m in tf:
                members += 1
                if members > MEMBERS_MAX:
                    raise Refused("the pack holds more than %d entries" % MEMBERS_MAX)
                parts = _parts(m.name)
                if m.name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", m.name) or ".." in parts:
                    raise Refused("the pack holds %r, a name outside its own folder" % m.name)
                if m.isdir():
                    continue
                if not m.isfile():
                    raise Refused("the pack holds %r, a link, device or pipe; only plain files and folders are allowed"
                                  % m.name)
                key = "/".join(parts)
                if key in seen:
                    raise Refused("the pack holds %r twice" % key)
                seen.add(key)
                files += 1
                total += m.size
                if total > UNPACKED_MAX:
                    raise Refused("the pack unpacks to more than %d GB" % (UNPACKED_MAX // 1024 ** 3))
                if key == "VERSION":
                    if m.size > VERSION_MAX:
                        raise Refused("the pack's VERSION file is larger than %d bytes" % VERSION_MAX)
                    found = tf.extractfile(m).read(VERSION_MAX).decode("utf-8", "replace").strip()
    except Refused:
        raise
    except (tarfile.TarError, OSError, EOFError, zlib.error) as e:
        raise Refused("the pack is not a whole gzip tar (%s)" % e)
    if found is None:
        raise Refused("the pack has no VERSION file at its top")
    if found != version:
        raise Refused("the pack's VERSION says %r, the request says %s" % (found, version))
    return files, total


def same(st, facts):
    return st.st_size == facts.get("size") and st.st_mtime_ns == facts.get("mtime_ns")


def mb(n):
    return "%.1f MB" % (n / 1e6) if isinstance(n, int) else "?"


# ---- the request ----------------------------------------------------------------------------------------------------
def _record(rid, key, version, sha, tests, now, cfg, previous):
    return {"id": rid, "action": "deploy", "kind": config.STAGED, "state": queue.PENDING,
            "repo": key, "sha": sha, "previous": previous, "subject": "", "version": version, "checks": "none",
            "tests": tests or "", "changelog": [], "files": None, "rules_changed": [],
            "seen": queue.stamp(now), "expires": queue.stamp(now + timedelta(hours=cfg.expire_hours)),
            "verified": {"on_main": False, "at": None}, "pack": None, "approval": None, "deploy": None}


def request(store, cfg, key, version, sha, tests=None, now=None, out=None):
    """zeus stage-request: one record, pending or refused with the reason (design §15.3). Returns (exit, record)."""
    now = now or queue.now_local()
    out = out or (lambda s: None)
    repo = cfg.repos[key]
    store.make_folders()
    deployed = (store.deployed(key) or {}).get("deployed") or {}
    previous = deployed.get("sha") if isinstance(deployed.get("sha"), str) and queue.SHA.match(deployed["sha"]) else None
    rec = _record(store.new_id(key.split("/")[-1], sha[:7], now), key, version, sha, tests, now, cfg, previous)

    def refuse(why):
        rec["state"], rec["reason"] = "refused", why
        store.write(rec)
        out(queue.line(rec))
        return 1, rec

    if not repo.staged:
        return refuse("%s is not a staged-pack repository in repos.json; its deploys come from main through "
                      "`zeus request`. Nothing was read." % key)
    for older in store.pending(queue.WAITING):
        if older["repo"] == key and older["sha"] == sha and older.get("version") == version:
            return refuse("already waiting as %s; nothing was read" % older["id"])
    try:
        pack, st, sumf = locate(repo, version, sha)
        checked_ns = time.time_ns()
        if st.st_mtime_ns > checked_ns:
            raise Refused("%s was modified in the future (its clock is ahead); stage it again" % pack.name)
        if st.st_size > PACK_MAX:
            raise Refused("the pack is larger than %d GB" % (PACK_MAX // 1024 ** 3))
        staged = read_sum(sumf, pack.name)
        got, size = digest(pack)
        if got != staged:
            raise Refused("the pack's sha256 is %s, but %s says %s; the pack is not the one that was staged"
                          % (got[:12], sumf.name, staged[:12]))
        files, unpacked = inspect(pack, version)
        if not same(os.lstat(pack), {"size": st.st_size, "mtime_ns": st.st_mtime_ns}) or size != st.st_size:
            raise Refused("%s changed while Zeus read it; stage it once, then ask" % pack.name)
    except Refused as e:
        return refuse(str(e))
    fetch = queue.Fetch(store, repo)
    if not fetch.run():
        return refuse(fetch.error)
    on_main = fetch.on_main(sha)
    rec["verified"] = {"on_main": on_main, "at": queue.stamp(queue.now_local())}
    if not on_main:
        return refuse("commit %s is not on %s of %s" % (sha[:7], config.BRANCH, key))
    commit_version = (commands._blob(fetch, sha, "VERSION") or "").strip()
    if commit_version != version:
        return refuse("commit %s's VERSION says %r, the request says %s" % (sha[:7], commit_version, version))
    code, subject, _ = queue.git(["log", "-1", "--format=%s", sha], cwd=fetch.where)
    rec["subject"] = subject.strip() if code == 0 else ""
    rec["changelog"] = commands.changelog_lines(commands._blob(fetch, sha, "CHANGELOG.md"))
    changed, paths = fetch.changed(previous, sha)
    rec["files"], rec["rules_changed"] = changed, [p for p in paths if p in repo.rules]
    rec["pack"] = {"name": pack.name, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": got, "files": files,
                   "unpacked": unpacked, "checked_ns": checked_ns}
    for older in store.pending(queue.WAITING):
        if older["repo"] == key and older["id"] != rec["id"]:
            older["state"] = "superseded"
            older["superseded_by"] = {"id": rec["id"], "version": version, "sha": sha}
            older["reason"] = "replaced by %s" % version
            store.write(older)
    store.write(rec)
    out(queue.line(rec))
    return 0, rec


# ---- the checks at the hold and before the install --------------------------------------------------------------------
def verify(rec, repo, copy_to=None):
    """The pack is still exactly the one the request recorded (design §15.4): same name, plain file, same size and
    modification time, not newer than the request, the same .sha256, and a sha256 recomputed now. With copy_to the
    bytes hashed are copied there, and the tar rules and VERSION are checked on that copy. Returns the sha256."""
    want = rec.get("pack") if isinstance(rec.get("pack"), dict) else None
    if not want or not isinstance(want.get("sha256"), str):
        raise Refused("the request has no recorded pack; it was not made by zeus stage-request")
    pack, st, sumf = locate(repo, rec.get("version"), rec.get("sha"))
    if pack.name != want.get("name"):
        raise Refused("the pack's name %s is not the one recorded (%s)" % (pack.name, want.get("name")))
    if not same(st, want):
        raise Refused("%s was replaced or changed after the request (now %d bytes, modified %d; recorded %s bytes, "
                      "modified %s)" % (pack.name, st.st_size, st.st_mtime_ns, want.get("size"), want.get("mtime_ns")))
    if not isinstance(want.get("checked_ns"), int) or st.st_mtime_ns > want["checked_ns"]:
        raise Refused("%s is newer than the request" % pack.name)
    if read_sum(sumf, pack.name) != want["sha256"]:
        raise Refused("%s changed after the request" % sumf.name)
    got, size = digest(pack, copy_to)
    if got != want["sha256"] or size != want["size"]:
        raise Refused("%s's sha256 is now %s, the request recorded %s" % (pack.name, got[:12], want["sha256"][:12]))
    if not same(os.lstat(pack), want):
        raise Refused("%s changed while Zeus read it" % pack.name)
    if copy_to is not None:
        files, _ = inspect(copy_to, rec["version"])
        if files != want.get("files"):
            raise Refused("Zeus's copy holds %d files, the request recorded %s" % (files, want.get("files")))
    return got


# ---- the command line ------------------------------------------------------------------------------------------------
def main(args):
    """zeus stage-request <repo> --version V --sha S [--tests "..."]: exit 0 pending · 1 refused · 2 bad input."""
    bad = bad_input(args.version, args.sha, args.tests)
    if bad:
        print("zeus stage-request: %s. Nothing was read." % "; ".join(bad), file=sys.stderr)
        return 2
    cfg = config.check(getattr(args, "file", None))
    if cfg.errors:
        print("zeus stage-request: %s has %d error%s; run `zeus check-config`." % (
            config.short(cfg.path), len(cfg.errors), "" if len(cfg.errors) == 1 else "s"), file=sys.stderr)
        return 1
    from zeus import approve                    # approve imports this module; resolve() is shared with zeus deploy
    key = approve.resolve(cfg, args.repo)
    if key is None:
        print("zeus stage-request: %s is not one repository in repos.json; nothing was read." % args.repo, file=sys.stderr)
        return 1
    code, rec = request(queue.Store(), cfg, key, args.version, args.sha, args.tests, out=print)
    return code
