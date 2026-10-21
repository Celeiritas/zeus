"""The signed decision log (docs/design.md §4): one JSON line per approval or decline in
~/.local/share/zeus/log/approvals-YYYY-MM.jsonl, each carrying an HMAC-SHA256 `sig` over its canonical JSON (keys
sorted, no spaces, without `sig`) keyed by ZEUS_LOG_KEY from secret.env. A line edited later no longer matches; `zeus
verify-log` (#7) re-checks every line. The signature is an audit trail, not an identity check: that is Janus's job."""
import hashlib
import hmac
import json

from zeus import init, queue


class LogError(Exception):
    """The log cannot be written: no key (run `zeus init`), or a loose secret.env."""


def canonical(entry):
    """The bytes the signature covers: the entry without sig, keys sorted, no spaces, UTF-8."""
    body = {k: v for k, v in entry.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def key():
    """The log key as bytes; LogError when it is missing or the file is readable by others."""
    try:
        hexkey = init.read_key()
    except init.SecretError as e:
        raise LogError(str(e))
    if not hexkey:
        raise LogError("%s: no %s; run `zeus init` once on this server." % (init.secret_path(), init.KEY_NAME))
    try:
        return bytes.fromhex(hexkey)
    except ValueError:
        raise LogError("%s: %s is not hex; move the file aside and run `zeus init` again."
                       % (init.secret_path(), init.KEY_NAME))


def sign(entry, k=None):
    """Return the entry with its sig."""
    signed = dict(entry)
    signed["sig"] = hmac.new(k or key(), canonical(entry), hashlib.sha256).hexdigest()
    return signed


def check(entry, k=None):
    """True when the entry's sig matches its content."""
    sig = entry.get("sig")
    if not isinstance(sig, str):
        return False
    want = hmac.new(k or key(), canonical(entry), hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, sig)


def path(store, kind="approvals", when=None):
    when = when or queue.now_local()
    return store.data / "log" / ("%s-%s.jsonl" % (kind, when.strftime("%Y-%m")))


def append(store, entry, kind="approvals", when=None):
    """Sign the entry and append it as one line to this month's file. Returns the signed entry."""
    signed = sign(entry)
    p = path(store, kind, when)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(signed, ensure_ascii=False, sort_keys=True) + "\n")
    return signed


def entries(store, kind="approvals"):
    """Every line of every monthly file of that kind, oldest first, as (file, line number, entry or None if broken)."""
    out = []
    folder = store.data / "log"
    if not folder.is_dir():
        return out
    for p in sorted(folder.glob("%s-*.jsonl" % kind)):
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                out.append((p, n, json.loads(line)))
            except ValueError:
                out.append((p, n, None))
    return out
