"""A fake systemd-run for Zeus's tests. It records its arguments (one JSON list per line) in ZEUS_FAKE_SYSTEMD_RUN_LOG
and exits 0, or exits 1 with a message when ZEUS_FAKE_SYSTEMD_RUN=fail. With ZEUS_FAKE_SYSTEMD_RUN=inline it runs
the command after the properties in the foreground and exits with its status (for the deploy tests, #8).
Run as: python tests/fixtures/systemd_run.py --user --unit=... -p ... <command...>."""
import json
import os
import subprocess
import sys


def main():
    args = sys.argv[1:]
    log = os.environ.get("ZEUS_FAKE_SYSTEMD_RUN_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(args) + "\n")
    mode = os.environ.get("ZEUS_FAKE_SYSTEMD_RUN", "ok")
    if mode == "fail":
        print("Failed to start transient service unit: Unit zeus-deploy-x.service already exists.", file=sys.stderr)
        return 1
    if mode == "inline":
        cmd, i = [], 0
        while i < len(args):
            if args[i] in ("-p", "--property"):
                i += 2
                continue
            if args[i].startswith("-"):
                i += 1
                continue
            cmd = args[i:]
            break
        return subprocess.run(cmd).returncode
    print("Running as unit: %s" % next((a[7:] for a in args if a.startswith("--unit=")), "?"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
