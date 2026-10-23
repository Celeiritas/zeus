#!/usr/bin/env python3
"""A stand-in zeus for the Throne page test (tests/throne/throne.test.js): it records every call in $ZEUS_DATA/calls.jsonl
and moves records the way Zeus would, so the page has something real to show. It decides nothing that matters."""
import json
import os
import sys
from pathlib import Path

D = Path(os.environ["ZEUS_DATA"])
args = sys.argv[1:]
with open(D / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(args) + "\n")


def opt(name):
    return args[args.index(name) + 1] if name in args else None


def move(rid, **change):
    src = D / "queue" / (rid + ".json")
    rec = json.loads(src.read_text(encoding="utf-8"))
    rec.update(change)
    (D / "queue" / ".done" / (rid + ".json")).write_text(json.dumps(rec), encoding="utf-8")
    src.unlink()
    return rec


cmd = args[0] if args else ""
if cmd == "approve":
    if not opt("--user"):
        sys.exit("zeus approve: no identity to check: open the Throne through tailscale serve.")
    rec = move(args[1], state="deploying", deploy={"started": True, "unit": "zeus-deploy-" + args[1]})
    (D / "log" / ("deploy-%s.log" % args[1])).write_text(
        "zeus: t deploying %s %s\nzeus: t checked out %s (detached)\nzeus: t running scripts/deploy.sh --no-pull\nbuilding the package\n"
        % (rec["repo"], rec["version"], rec["sha"][:7]), encoding="utf-8")
    print("approved %s %s; the deploy runs as zeus-deploy-%s" % (rec["repo"], rec["version"], args[1]))
elif cmd == "decline":
    rec = move(args[1], state="declined", reason=opt("--reason"))
    print("%s: declined (%s)" % (args[1], opt("--reason")))
elif cmd == "rollback":
    print("rolling %s back to %s; the run starts now" % (args[1], (opt("--sha") or "")[:7]))
elif cmd == "request":
    print("%s: nothing new on main" % args[1])
elif cmd == "seen":
    p = Path(os.environ["ZEUS_ALERT"])
    if p.exists():
        p.unlink()
    print("the alert is cleared")
else:
    sys.exit("fake zeus: unknown command %s" % cmd)
