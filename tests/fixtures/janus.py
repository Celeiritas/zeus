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
    # ZEUS_FAKE_JANUS_NODES: {"<ip>": ["<node>", "<whois user>"]} - the approved devices. When set, whois decides:
    # an address not in it is refused, and a user given in the request must equal the whois user (as Janus checks).
    # As the real Janus: "via" is elaia unless the request says ssh, and only an ssh request may leave the user out.
    if mode == "allowed" and req.get("user") is None and req.get("via", "elaia") != "ssh":
        print(json.dumps({"verdict": "refused", "node": None, "user": None, "action": req.get("action"),
                          "reasons": ["A request from the page must name the user it saw; only an ssh approval "
                                      "leaves the user to Janus."]}))
        return 10
    nodes = json.loads(os.environ.get("ZEUS_FAKE_JANUS_NODES") or "null")
    node, who = "phone", req.get("user") or "owner@example.com"
    if nodes is not None and mode == "allowed":
        if req.get("ip") not in nodes:
            mode = "refused"
        else:
            node, who = nodes[req["ip"]]
            if req.get("user") not in (None, who):
                mode = "refused"
    reasons = {"allowed": [], "refused": ["%s is not in approvers.zeus.users" % req.get("user"),
                                         "node for %s is not in approvers.zeus.nodes" % req.get("ip")],
               "failed": ["tailscale whois failed"]}[mode]
    print(json.dumps({"verdict": mode, "reasons": reasons, "node": node if mode == "allowed" else None,
                      "user": who if mode == "allowed" else None, "action": req.get("action")}))
    return EXIT[mode]


if __name__ == "__main__":
    sys.exit(main())
