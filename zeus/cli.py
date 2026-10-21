"""The zeus command line. Each issue adds its command here; the ones not built yet exit 2 and say so."""
import argparse
import sys

from zeus import alert, approve, commands, config, deploy, init, queue, rollback, verify, version

DESCRIPTION = ("Zeus deploys a commit only after the owner approved it, and can put the previous approved commit back. "
               "He never fetches from anywhere but the allowlisted repositories, never deploys a branch, never runs a "
               "command from a message, a page or a config, and never pushes anywhere.")
# Commands later issues build (docs/design.md §13); each says which issue, so a stub is never mistaken for a bug.
LATER = (
    ("approve-release", "approve a release of a quarantined Hermes arrival", "#12"),
)


def check_config(args):
    """zeus check-config [--file F]: one sentence per repository, then the warnings and errors; exit 1 on errors."""
    cfg = config.check(args.file)
    for line in cfg.explain():
        print(line)
    for w in cfg.warnings:
        print("warning: " + w, file=sys.stderr)
    for e in cfg.errors:
        print("error: " + e, file=sys.stderr)
    n, w = len(cfg.repos), len(cfg.warnings)
    if cfg.errors:
        print("%s: %d error%s. Fix them; Zeus deploys nothing until repos.json is right."
              % (config.short(cfg.path), len(cfg.errors), "" if len(cfg.errors) == 1 else "s"), file=sys.stderr)
        return 1
    print("%s: %d repositor%s ok%s" % (config.short(cfg.path), n, "y" if n == 1 else "ies",
                                      ", %d warning%s" % (w, "" if w == 1 else "s") if w else ""))
    return 0


def not_built(name, issue):
    def run(args):
        print("zeus %s: not built yet (issue %s)." % (name, issue), file=sys.stderr)
        return 2
    return run


def build_parser():
    parser = argparse.ArgumentParser(prog="zeus", description=DESCRIPTION)
    parser.add_argument("--version", action="version", version="zeus " + version())
    sub = parser.add_subparsers(dest="command", title="commands", metavar="<command>")
    p = sub.add_parser("check-config", help="read repos.json, say what every repository deploys, and list what is wrong")
    p.add_argument("--file", metavar="FILE", help="the allowlist to check (default: $ZEUS_CONFIG or " + config.CONFIG + ")")
    p.set_defaults(func=check_config)
    p = sub.add_parser("queue", help="turn the request messages in requests/ into records (zeus-queue.service)")
    p.add_argument("--file", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=queue.main)
    p = sub.add_parser("request", help="make a record now for the current main of a repository (the Throne's Re-check)")
    p.add_argument("repo", help="owner/name, as in repos.json")
    p.add_argument("--file", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=commands.main_request)
    p = sub.add_parser("decline", help="mark a waiting request declined, with a reason word; no Janus call, one signed line")
    p.add_argument("id")
    p.add_argument("--reason", required=True, metavar="WORD", help="one word, e.g. not-now")
    p.add_argument("--user", help=argparse.SUPPRESS)   # the Throne passes the tailnet login; from a shell it is $USER
    p.add_argument("--ip", help=argparse.SUPPRESS)
    p.set_defaults(func=commands.main_decline)
    p = sub.add_parser("approve", help="approve one request for one commit, once: fresh, re-checked on main, verified by "
                                      "Janus, signed into the log, then deployed under systemd-run")
    p.add_argument("id")
    p.add_argument("--sha", help="the commit the page showed (40 hex): required from the Throne; over ssh the prompt shows it")
    p.add_argument("--user", help="from the Throne only: the tailnet login (Tailscale-User-Login); refused over ssh")
    p.add_argument("--ip", help="from the Throne only: the client's tailnet address (X-Forwarded-For); refused over ssh")
    p.add_argument("--file", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=approve.main)
    p = sub.add_parser("deploy", help="request the current main of a repository and approve it in one line, e.g. "
                                     "ssh {{SSH}} 'zeus deploy argus-ui' (asks you to type yes)")
    p.add_argument("repo", help="argus-ui or you/argus-ui, as in repos.json")
    p.add_argument("--user", help=argparse.SUPPRESS)
    p.add_argument("--ip", help=argparse.SUPPRESS)
    p.add_argument("--file", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=approve.main_deploy)
    p = sub.add_parser("status", help="what waits, what is deploying, what is live, and the last ten history rows")
    p.add_argument("--json", action="store_true", help="the document the Throne plugin reads (docs/contract.md)")
    p.add_argument("--file", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=commands.main_status)
    p = sub.add_parser("rollback", help="put the previous approved commit back (Janus verifies, signed line, same deploy run): "
                                       "ssh {{SSH}} 'zeus rollback argus-ui'")
    p.add_argument("repo", help="argus-ui or you/argus-ui")
    p.add_argument("--sha", help="from the Throne: the target the page showed; over ssh the prompt shows it")
    p.add_argument("--user", help=argparse.SUPPRESS)
    p.add_argument("--ip", help=argparse.SUPPRESS)
    p.add_argument("--file", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=rollback.main)
    p = sub.add_parser("deploy-run", help="(internal, started by approve under systemd-run) the deploy of one approved request")
    p.add_argument("id")
    p.add_argument("--file", metavar="FILE", help=argparse.SUPPRESS)
    p.set_defaults(func=deploy.main)
    p = sub.add_parser("verify-log", help="re-check the signature of every line in the approvals logs; exit 10 if any is bad")
    p.add_argument("path", nargs="?", help="a log file, or a folder of approvals-*.jsonl (default ~/.local/share/zeus/log/; "
                                           "the journal copy in the config repo works too)")
    p.add_argument("--key-file", metavar="FILE", help="the secret.env holding ZEUS_LOG_KEY (default ~/.config/zeus/secret.env)")
    p.add_argument("--quiet", action="store_true", help="print only the bad lines and the total")
    p.set_defaults(func=verify.main)
    p = sub.add_parser("seen", help="the owner has read Zeus's alert: clear it (Argus calms on his next refresh)")
    p.set_defaults(func=alert.main_seen)
    p = sub.add_parser("init", help="make the log signing key (secret.env, mode 600) and the data folders; never overwrites")
    p.set_defaults(func=lambda a: init.run(print))
    for name, help_text, issue in LATER:
        p = sub.add_parser(name, help=help_text + " (not built yet, issue %s)" % issue)
        p.add_argument("args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
        p.set_defaults(func=not_built(name, issue))
    return parser


def main(argv=None):
    """Run the command line; returns the exit status."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)
