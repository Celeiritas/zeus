"""A fake Janus for Zeus's tests (janus docs/design.md §4): `janus verify-approval` reads one JSON request on stdin
and answers with exit 0 allowed · 10 refused · 2 failed, printing one JSON verdict. The verdict comes from
ZEUS_FAKE_JANUS: allowed (default) | refused | failed | hang (never answers) | garbage (exit 0, not JSON) |
plain (exit 10, reasons as text lines). The request it received is appended to ZEUS_FAKE_JANUS_LOG, one per line,
so a test can see exactly what Zeus asked. Run as: python tests/fixtures/janus.py verify-approval."""
import json
import os
import sys
import time

EXIT = {"allowed": 0, "refused": 10, "failed": 2}


def main():
    if sys.argv[1:] != ["verify-approval"]:
        print("fake janus: only verify-approval is faked", file=sys.stderr)
        return 3
    raw = sys.stdin.read()
    log = os.environ.get("ZEUS_FAKE_JANUS_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(raw.strip() + "\n")
    mode = os.environ.get("ZEUS_FAKE_JANUS", "allowed")
    if mode == "hang":
        time.sleep(600)
    if mode == "garbage":
        print("ok whatever")
        return 0
    if mode == "plain":
        print("user is not an approver")
        print("node is not approved", file=sys.stderr)
        return 10
    try:
        req = json.loads(raw)
    except ValueError:
        print(json.dumps({"verdict": "failed", "reasons": ["request is not JSON"]}))
        return 2
    reasons = {"allowed": [], "refused": ["%s is not in approvers.zeus.users" % req.get("user"),
                                         "node for %s is not in approvers.zeus.nodes" % req.get("ip")],
               "failed": ["tailscale whois failed"]}[mode]
    print(json.dumps({"verdict": mode, "reasons": reasons, "node": "phone" if mode == "allowed" else None,
                      "action": req.get("action")}))
    return EXIT[mode]


if __name__ == "__main__":
    sys.exit(main())
