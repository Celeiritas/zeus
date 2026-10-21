"""`zeus init`: the one-time setup on the server (docs/design.md §2 "Bootstrap" and §4). Makes the signing key for the
approval log in ~/.config/zeus/secret.env (mode 600, 32 random bytes as hex) and the data folders Zeus writes into.
It never overwrites a secret that exists: the signatures in every log line so far depend on it. A secret other users
can read is a secret they can sign with, so read_key refuses one (and init tightens the mode of a kept file)."""
import os
import secrets
import stat
from pathlib import Path

SECRET = "~/.config/zeus/secret.env"          # or $ZEUS_SECRET
DATA = "~/.local/share/zeus"                  # or $ZEUS_DATA
KEY_NAME = "ZEUS_LOG_KEY"
KEY_BYTES = 32
FOLDERS = ("requests", "queue", "queue/.done", "state", "log")


def secret_path():
    return Path(os.path.expanduser(os.environ.get("ZEUS_SECRET") or SECRET))


def data_path():
    return Path(os.path.expanduser(os.environ.get("ZEUS_DATA") or DATA))


class SecretError(Exception):
    """secret.env is readable by other users; the fix is in the message."""


def loose(path):
    """True when other users may read the file (not checked on Windows, where the mode means nothing)."""
    return os.name != "nt" and bool(stat.S_IMODE(path.stat().st_mode) & 0o077)


def read_key(path=None):
    """ZEUS_LOG_KEY from secret.env, or None when the file or the line is missing. Raises SecretError when other users
    can read the file: a key they can read is a key they can sign the log with."""
    path = path or secret_path()
    try:
        if loose(path):
            raise SecretError("%s: readable by other users, who could sign the approval log with it. chmod 600 %s."
                              % (path, path))
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(KEY_NAME + "="):
                return line[len(KEY_NAME) + 1:].strip() or None
    except OSError:
        pass
    return None


def make_secret(path):
    """Write a fresh secret.env, readable by its owner only. Returns False when one already exists (kept as it is)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("# Zeus's signing key for the approval log (zeus init). Keep it: every signed line depends on it.\n")
        f.write("%s=%s\n" % (KEY_NAME, secrets.token_hex(KEY_BYTES)))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return True


def run(out):
    """zeus init: make secret.env and the data folders; say what was made and what was already there."""
    secret, data = secret_path(), data_path()
    made = make_secret(secret)
    fixed = False
    if not made and loose(secret):
        os.chmod(secret, 0o600)
        fixed = True
    out("%s: %s" % (secret, "made (mode 600)" if made else "already exists, kept" + ("; mode fixed to 600" if fixed else "")))
    if not made and read_key(secret) is None:
        out("warning: %s has no %s line; move the file aside and run `zeus init` again." % (secret, KEY_NAME))
        return 1
    new = []
    for name in FOLDERS:
        folder = data / name
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
            new.append(name)
    out("%s: %s" % (data, "made " + ", ".join(new) if new else "folders already there"))
    return 0
