"""repos.json: the allowlist (docs/design.md §2). Load it, expand `~` and {{PLACEHOLDERS}} from realm.json, validate
every field, and explain it in plain sentences (`zeus check-config`). The branch (`main`) and the deploy script
(`scripts/deploy.sh`) are fixed in code: a file that tries to name either fails here, so no config can widen Zeus."""
import json
import os
import re
from pathlib import Path

CONFIG = "~/.config/zeus/repos.json"          # or $ZEUS_CONFIG, or `--file`
REALM = "~/.config/leafy/realm.json"          # or $LEAFY_REALM: the same file Mentor and Hermes read
SSH_CONFIG = "~/.ssh/config"
REALM_DEFAULTS = {"SERVER": "homeserver", "PC": "desktop", "PHONE": "phone", "SSH": "server",
                  "HOME_PC": "C:\\Users\\you", "HOME_SERVER": "~", "REPO_URL": "https://github.com/you/argus-ui"}
PLACEHOLDER = re.compile(r"\{\{\s*(" + "|".join(REALM_DEFAULTS) + r")\s*\}\}")

BRANCH = "main"                               # fixed in code, never in config
DEPLOY_SCRIPT = "scripts/deploy.sh"           # fixed in code, never in config
HEALTH_EXEC = ("hermes check-config", "janus check-config", "zeus check-config")   # the only commands `exec` may name
HEALTH_SHAPES = ("http", "service", "exec")
HEALTH_HTTP = re.compile(r"^http://127\.0\.0\.1(:\d{1,5})?(/.*)?$")
UNIT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._:-]{0,255}$")
NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
REPO_KEY = re.compile(r"^(%s)/(%s)$" % (NAME, NAME))
URL_HTTPS = re.compile(r"^https://github\.com/(%s)/(%s)\.git$" % (NAME, NAME))
URL_SSH = re.compile(r"^git@(%s):(%s)/(%s)\.git$" % (NAME, NAME, NAME))
DEFAULTS = {"expire_hours": 72, "deploy_timeout_s": 900, "health_wait_s": 30}
TOP_KEYS = ("version", "repos") + tuple(DEFAULTS)
REPO_KEYS = ("url", "path", "key", "private", "health", "services", "rules")
# Settings a config must never carry: each would let a file, not the code, choose what Zeus runs.
FORBIDDEN = {
    "branch": "Zeus deploys %s only; the branch is fixed in code." % BRANCH,
    "script": "Zeus runs %s of the checked-out commit only; the script is fixed in code." % DEPLOY_SCRIPT,
    "deploy": "Zeus runs %s of the checked-out commit only; a config never names a command." % DEPLOY_SCRIPT,
    "command": "a config never names a command.",
    "run": "a config never names a command.",
    "exec": "a config never names a command (health.exec is the one place, from a fixed list).",
    "hooks": "git hooks are disabled in Zeus's clones; a config never names a command.",
}


def config_path():
    return Path(os.path.expanduser(os.environ.get("ZEUS_CONFIG") or CONFIG))


def realm_path():
    return Path(os.path.expanduser(os.environ.get("LEAFY_REALM") or REALM))


def realm(path=None):
    """The placeholder values: realm.json over the public defaults. A missing or broken file means the defaults."""
    try:
        with open(path or realm_path(), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        doc = {}
    vals = dict(REALM_DEFAULTS)
    for k, v in (doc.items() if isinstance(doc, dict) else []):
        if str(k).upper() in vals and isinstance(v, (str, int, float)) and str(v).strip():
            vals[str(k).upper()] = str(v)
    return vals


def fill(text, vals, as_json=False):
    """Put the realm's values where {{SERVER}} etc. sit. as_json: text is JSON, so each value goes in escaped."""
    if "{{" not in text:
        return text
    return PLACEHOLDER.sub(lambda m: json.dumps(vals[m.group(1)])[1:-1] if as_json else vals[m.group(1)], text)


def short(path):
    """A path for people: the home folder shown as ~."""
    try:
        return "~/" + Path(path).relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def ssh_aliases(path=None):
    """The Host names in ~/.ssh/config (empty when there is no such file)."""
    hosts = set()
    try:
        for line in Path(path or os.path.expanduser(SSH_CONFIG)).read_text(encoding="utf-8").splitlines():
            words = line.strip().split()
            if len(words) >= 2 and words[0].lower() == "host":
                hosts.update(words[1:])
    except OSError:
        pass
    return hosts


class ConfigError(Exception):
    """repos.json has errors; `zeus check-config` lists them."""
    def __init__(self, config):
        super().__init__("%s: %d error%s; run `zeus check-config`" % (short(config.path), len(config.errors),
                                                                       "" if len(config.errors) == 1 else "s"))
        self.config = config


class Repo:
    """One allowlisted repository after validation."""
    def __init__(self, key, raw):
        self.key, self.raw = key, raw
        self.url = raw.get("url")
        self.path = Path(os.path.expanduser(raw["path"])) if isinstance(raw.get("path"), str) else None
        self.deploy_key = Path(os.path.expanduser(raw["key"])) if isinstance(raw.get("key"), str) else None
        self.private = raw.get("private") is True
        self.health = raw.get("health") if isinstance(raw.get("health"), dict) else {}
        self.services = list(raw.get("services") or []) if isinstance(raw.get("services"), list) else []
        self.rules = list(raw.get("rules") or []) if isinstance(raw.get("rules"), list) else []

    @property
    def ssh(self):
        return isinstance(self.url, str) and self.url.startswith("git@")

    def sentence(self):
        """One plain sentence: what Zeus would deploy for this repository, and how he checks it came up."""
        s = "%s: %s of %s into %s" % (self.key, BRANCH, self.url, short(self.path) if self.path else "?")
        if self.private:
            s += " (private, key %s)" % (short(self.deploy_key) if self.deploy_key else "missing")
        s += ", then %s" % DEPLOY_SCRIPT
        if "http" in self.health:
            s += "; healthy when GET %s answers 200" % self.health["http"]
        elif "service" in self.health:
            s += "; healthy when %s is active" % self.health["service"]
        elif "exec" in self.health:
            s += "; healthy when `%s` passes" % self.health["exec"]
        else:
            s += "; health check broken, see the errors"
        if self.services:
            s += "; Argus watches %s" % ", ".join(map(str, self.services))
        if self.rules:
            s += "; a change to %s is flagged as changing the realm's own rules" % ", ".join(map(str, self.rules))
        return s + "."


class Config:
    """A read repos.json: the settings and repositories, plus the errors and warnings found."""
    def __init__(self, path):
        self.path = path
        self.repos = {}
        self.expire_hours = DEFAULTS["expire_hours"]
        self.deploy_timeout_s = DEFAULTS["deploy_timeout_s"]
        self.health_wait_s = DEFAULTS["health_wait_s"]
        self.errors, self.warnings = [], []

    @property
    def ok(self):
        return not self.errors

    def explain(self):
        return [r.sentence() for r in self.repos.values()]


def check(path=None, vals=None):
    """Read and validate repos.json without raising: the Config carries the errors and warnings, in plain words."""
    cfg = Config(Path(os.path.expanduser(path)) if path else config_path())
    err, warn = cfg.errors.append, cfg.warnings.append
    try:
        text = cfg.path.read_text(encoding="utf-8")
    except OSError as e:
        err("%s: cannot read it (%s). Copy examples/repos.example.json there and fill it in, or point --file or "
            "ZEUS_CONFIG at your file." % (short(cfg.path), e.strerror))
        return cfg
    try:
        doc = json.loads(fill(text, vals or realm(), as_json=True))
    except ValueError as e:
        err("%s: not valid JSON (%s). Look for a missing comma, quote or bracket near that line." % (short(cfg.path), e))
        return cfg
    if not isinstance(doc, dict):
        err("%s: the file must hold one JSON object, like examples/repos.example.json." % short(cfg.path))
        return cfg
    if doc.get("version") != 1:
        err("version: must be 1 (the only repos.json version this Zeus reads), not %r." % (doc.get("version"),))
    for k in sorted(doc):
        if k in FORBIDDEN:
            err("%s: not a setting; %s Remove it." % (k, FORBIDDEN[k]))
        elif k not in TOP_KEYS:
            warn("%s: unknown setting, ignored (the settings are %s)." % (k, ", ".join(TOP_KEYS)))
    for k, default in DEFAULTS.items():
        v = doc.get(k, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            err("%s: must be a number above 0 (default %s), not %r." % (k, default, v))
        else:
            setattr(cfg, k, v)

    repos = doc.get("repos")
    if not isinstance(repos, dict) or not repos:
        err('repos: must map at least one "owner/name" to a repository, e.g. "you/argus-ui": { "url": ..., "path": ..., '
            '"health": ... }.')
        return cfg
    paths = {}
    for key, raw in repos.items():
        _check_repo(cfg, str(key), raw, paths)
    return cfg


def _check_repo(cfg, key, raw, paths):
    err, warn = cfg.errors.append, cfg.warnings.append
    m = REPO_KEY.match(key)
    if not m:
        err("repos: %r is not a repository name; use \"owner/name\" as on GitHub, e.g. you/argus-ui." % key)
        return
    if not isinstance(raw, dict):
        err('repos.%s: must be an object with "url", "path" and "health".' % key)
        return
    for k in sorted(raw):
        if k in FORBIDDEN:
            err("repos.%s.%s: not a setting; %s Remove it." % (key, k, FORBIDDEN[k]))
        elif k not in REPO_KEYS:
            warn("repos.%s: unknown setting %r is ignored (the settings are %s)." % (key, k, ", ".join(REPO_KEYS)))
    r = Repo(key, raw)

    url = raw.get("url")
    if not isinstance(url, str) or not url.strip():
        err('repos.%s: "url" is missing. Give https://github.com/%s.git, or git@<ssh alias>:%s.git for a private '
            "repository." % (key, key, key))
    elif URL_HTTPS.match(url) or URL_SSH.match(url):
        owner, name = (URL_HTTPS.match(url) or URL_SSH.match(url)).groups()[-2:]
        if "%s/%s" % (owner, name) != key:
            err('repos.%s: "url" %s names %s/%s, but the key says %s. The key must equal the owner/name in the url.'
                % (key, url, owner, name, key))
        if r.ssh:
            alias = URL_SSH.match(url).group(1)
            if not r.private:
                err('repos.%s: "url" %s is an ssh alias, which is for private repositories only. Set "private": true '
                    "and give the read-only deploy key under \"key\", or use https://github.com/%s.git." % (key, url, key))
            elif alias != "github.com" and alias not in ssh_aliases():
                warn("repos.%s: ssh alias %s is not a Host in %s, so a fetch would fail until it is added there."
                     % (key, alias, SSH_CONFIG))
        elif r.private:
            err('repos.%s: "private": true needs an ssh alias url (git@<alias>:%s.git) and a deploy key; Zeus has no '
                "password for %s." % (key, key, url))
    else:
        err('repos.%s: "url" must be https://github.com/<owner>/<name>.git or git@<ssh alias>:<owner>/<name>.git, '
            "not %s. Zeus fetches from GitHub only." % (key, url))

    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        err('repos.%s: "path" is missing. Give the folder of the clone on the server, e.g. ~/src/%s.' % (key, m.group(2)))
    elif not (path.startswith(("/", "~/")) or path == "~" or r.path.is_absolute()):   # the server is Linux; a PC may check
        err('repos.%s: "path" %s must be an absolute path or start with ~ (Zeus makes the clone there on the first '
            "deploy)." % (key, path))
    elif r.path in paths:
        err('repos.%s: "path" %s is already the clone of %s. Give every repository its own folder.'
            % (key, short(r.path), paths[r.path]))
    else:
        paths[r.path] = key
        if r.path.exists() and not r.path.is_dir():
            err('repos.%s: "path" %s exists and is not a folder.' % (key, short(r.path)))

    if "private" in raw and not isinstance(raw["private"], bool):
        err('repos.%s: "private" must be true or false, not %r.' % (key, raw["private"]))
    deploy_key = raw.get("key")
    if "key" in raw and (not isinstance(deploy_key, str) or not deploy_key.strip()):
        err('repos.%s: "key" must be the path of Zeus\'s read-only deploy key, e.g. ~/.config/zeus/keys/%s.'
            % (key, m.group(2)))
    elif r.private and r.deploy_key is None:
        err('repos.%s: a private repository needs "key", the path of Zeus\'s read-only deploy key for it.' % key)
    elif r.deploy_key is not None and not r.private:
        warn("repos.%s: \"key\" is only used for a private repository; this one is public, so it is ignored." % key)
    elif r.deploy_key is not None and not r.deploy_key.is_file():
        warn("repos.%s: deploy key %s does not exist yet; make it before the first deploy." % (key, short(r.deploy_key)))

    _check_health(cfg, key, raw.get("health"))
    for field in ("services", "rules"):
        if field not in raw:
            continue
        items = raw.get(field)
        example = '["argus-ui"]' if field == "services" else '["zeus/repos.json"]'
        if not isinstance(items, list) or not all(isinstance(i, str) and i.strip() for i in items):
            err('repos.%s: "%s" must be a list of %s, e.g. %s.' % (key, field, "unit names" if field == "services"
                                                                 else "paths inside the repository", example))
        elif field == "services" and not all(UNIT.match(i) for i in items):
            err('repos.%s: "services" must hold systemd unit names, like argus-ui, not %r.' % (key, items))
        elif field == "rules" and any(i.startswith("/") or ".." in i.split("/") for i in items):
            err('repos.%s: "rules" are paths inside the repository (no leading / and no ..), not %r.' % (key, items))
    cfg.repos[key] = r


def _check_health(cfg, key, health):
    err = cfg.errors.append
    if not isinstance(health, dict):
        err('repos.%s: "health" is missing. Give one of { "http": "http://127.0.0.1:<port>/..." }, { "service": "<unit>" } '
            'or { "exec": "<agent> check-config" }.' % key)
        return
    shapes = [s for s in HEALTH_SHAPES if s in health]
    extra = sorted(set(health) - set(HEALTH_SHAPES))
    if len(shapes) != 1 or extra:
        err('repos.%s: "health" must be exactly one of http, service or exec, not %s.'
            % (key, ", ".join(sorted(health)) or "nothing"))
        return
    shape = shapes[0]
    value = health[shape]
    if shape == "http" and (not isinstance(value, str) or not HEALTH_HTTP.match(value)):
        err('repos.%s: "health.http" must be a URL on this machine, http://127.0.0.1:<port>/..., not %r. Zeus checks '
            "health on the server only." % (key, value))
    elif shape == "service" and (not isinstance(value, str) or not UNIT.match(value)):
        err('repos.%s: "health.service" must be a systemd user unit name, like argus-ui, not %r.' % (key, value))
    elif shape == "exec" and value not in HEALTH_EXEC:
        err('repos.%s: "health.exec" must be one of %s, not %r. Zeus never runs a command named in config.'
            % (key, ", ".join("`%s`" % c for c in HEALTH_EXEC), value))


def load(path=None):
    """Read and validate repos.json for the commands that deploy; raises ConfigError when it has errors."""
    cfg = check(path)
    if cfg.errors:
        raise ConfigError(cfg)
    return cfg
