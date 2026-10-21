"""zeus verify-log (docs/design.md §4, docs/contract.md §4; issue #7): re-check the HMAC of every line in the
approvals logs, Zeus's own (~/.local/share/zeus/log/) or the journal copy in the private config repo. A line whose
signature no longer matches was changed after Zeus wrote it, or was not written with this key. The check proves the
line is unchanged since signing; it does not prove who approved (that was Janus's check at the time)."""
import json
import os
import sys
from pathlib import Path

from zeus import init, log, queue

BAD_EXIT = 10


def files(target):
    """The approvals-*.jsonl files to check: one file, or every one in a folder (oldest month first)."""
    target = Path(os.path.expanduser(str(target)))
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(target.glob("approvals-*.jsonl"))
    return None


def verify(paths, key):
    """[(file, line number, "ok" | "bad", why)] for every non-empty line."""
    rows = []
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            rows.append((p, 0, "bad", "cannot read the file (%s)" % (getattr(e, "strerror", None) or e)))
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                rows.append((p, n, "bad", "not JSON"))
                continue
            if not isinstance(entry, dict):
                rows.append((p, n, "bad", "not a JSON object"))
            elif not isinstance(entry.get("sig"), str):
                rows.append((p, n, "bad", "no sig"))
            elif not log.check(entry, key):
                rows.append((p, n, "bad", "sig does not match: the line was changed after signing, or signed with "
                                          "another key"))
            else:
                rows.append((p, n, "ok", "%s %s %s" % (entry.get("action", "?"), entry.get("id", "?"),
                                                       (entry.get("sha") or "")[:7])))
    return rows


def main(args):
    target = args.path or (queue.Store().data / "log")
    paths = files(target)
    if paths is None:
        print("zeus verify-log: %s is neither a file nor a folder." % target, file=sys.stderr)
        return 1
    try:
        hexkey = init.read_key(Path(os.path.expanduser(args.key_file)) if args.key_file else None)
    except init.SecretError as e:
        print("zeus verify-log: %s" % e, file=sys.stderr)
        return 1
    if not hexkey:
        print("zeus verify-log: no %s in %s; the key that signed the lines is needed to check them."
              % (init.KEY_NAME, args.key_file or init.secret_path()), file=sys.stderr)
        return 1
    try:
        key = bytes.fromhex(hexkey)
    except ValueError:
        print("zeus verify-log: %s is not hex." % init.KEY_NAME, file=sys.stderr)
        return 1
    rows = verify(paths, key)
    bad = [r for r in rows if r[2] == "bad"]
    for p, n, verdict, why in rows:
        if verdict == "bad" or not args.quiet:
            print("%s:%d: %s (%s)" % (p.name, n, verdict, why))
    print("zeus verify-log: %d line%s in %d file%s, %d ok, %d bad"
          % (len(rows), "" if len(rows) == 1 else "s", len(paths), "" if len(paths) == 1 else "s",
             len(rows) - len(bad), len(bad)))
    return BAD_EXIT if bad else 0
