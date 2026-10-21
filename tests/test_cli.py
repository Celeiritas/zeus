"""The zeus command line and its version (python -m zeus from the checkout). The commands later issues build exist
already and say so, so a typo and a missing feature look different."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

import zeus
from zeus import cli

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()


def run(*args):
    return subprocess.run([sys.executable, "-m", "zeus", *args], cwd=ROOT, capture_output=True, text=True)


class CliTest(unittest.TestCase):
    def test_version_matches_the_version_file(self):
        self.assertEqual(zeus.version(), VERSION)
        self.assertRegex(VERSION, r"^\d+\.\d+\.\d+$")

    def test_version_flag(self):
        r = run("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "zeus " + VERSION)

    def test_no_command_prints_help(self):
        r = run()
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: zeus", r.stdout)
        self.assertIn("never deploys a branch", r.stdout)

    def test_unknown_command_is_refused(self):
        r = run("push")
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage: zeus", r.stderr)

    def test_every_subcommand_from_the_design_exists(self):
        wanted = {"queue", "request", "approve", "approve-release", "decline", "deploy", "deploy-run", "seen", "rollback", "status",
                  "check-config", "init", "verify-log"}
        parser = cli.build_parser()
        names = {name for action in parser._subparsers._group_actions for name in action.choices}
        self.assertEqual(wanted, names)

    def test_commands_of_later_issues_say_not_built_yet(self):
        for name, _, issue in cli.LATER:
            r = run(name, "some-id", "--sha", "abc")
            self.assertEqual(r.returncode, 2, name)
            self.assertIn("zeus %s: not built yet (issue %s)" % (name, issue), r.stderr)

    def test_help_lists_every_command_with_a_line(self):
        r = run("--help")
        for name in ("check-config", "init", "approve", "rollback", "verify-log"):
            self.assertIn(name, r.stdout)


if __name__ == "__main__":
    unittest.main()
