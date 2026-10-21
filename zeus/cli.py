"""The zeus command line. Each issue adds its command here; the ones not built yet exit 2 and say so."""
import argparse
import sys

from zeus import config, init, version

DESCRIPTION = ("Zeus deploys a commit only after the owner approved it, and can put the previous approved commit back. "
               "He never fetches from anywhere but the allowlisted repositories, never deploys a branch, never runs a "
               "command from a message, a page or a config, and never pushes anywhere.")
# Commands later issues build (docs/design.md §13); each says which issue, so a stub is never mistaken for a bug.
LATER = (
    ("queue", "turn the request messages in requests/ into records (zeus-queue.service)", "#3"),
    ("request", "make a record now for the current main of a repository", "#4"),
    ("decline", "mark a waiting request declined, with a reason word", "#4"),
    ("status", "what waits, what is deploying, what is live", "#4"),
    ("approve", "approve one request for one commit, once; verified by Janus and signed into the log", "#5"),
    ("verify-log", "re-check the signature of every approval line", "#7"),
    ("deploy-run", "(internal) the deploy itself, under systemd-run", "#8"),
    ("rollback", "put the previous approved commit back", "#9"),
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
