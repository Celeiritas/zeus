"""Canonical JSON, strict parsing, base64url, the challenge, key refs and run ids (docs/contract.md §8b).

Pure functions: no file, clock or command is touched here, so all of this runs and is tested on any machine.

The canonical JSON here is ASCII-only (`ensure_ascii=True`) because the key page recomputes the same bytes in the
browser. It is not Zeus's log canonical (zeus/log.py, `ensure_ascii=False`) and shares no code with it. Every field
that goes into a challenge is first held to a narrow ASCII pattern, so Python and the page can't serialise the same
record two ways (Review Focus 4): an odd field is refused as malformed before anything is hashed.

The patterns are compiled and used with `fullmatch`, never `^...$` (`$` also matches before a final newline).
"""
import base64
import binascii
import hashlib
import json
import math
import re


class Malformed(ValueError):
    """A record, field or file without exactly the expected shape: refused before anything else looks at it."""


NONCE_RE = re.compile(r"[A-Za-z0-9_-]{43}")        # 32 random bytes in base64url without padding
ID_RE = re.compile(r"[a-z0-9-]{1,40}")              # an action id
HEX64_RE = re.compile(r"[0-9a-f]{64}")              # a SHA-256 in lowercase hex (the list's hash)
REF_RE = re.compile(r"[0-9a-f]{16}")                # a key ref (key_ref)
B64U_RE = re.compile(r"[A-Za-z0-9_-]*")
# The key page's exact origin: https, a lowercase DNS name, and always a port (the key page never sits on 443).
ORIGIN_RE = re.compile(r"https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
                       r":(?P<port>[1-9][0-9]{0,4})")
CHECK_KEY = "check-key"
MAX_SAFE_INT = 2 ** 53 - 1                          # the largest integer the page's JavaScript holds exactly


def canonical(obj):
    """Keys sorted, no spaces, ASCII only (anything else as \\uXXXX). NaN and infinities raise ValueError."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def _no_repeats(pairs):
    obj = {}
    for k, v in pairs:
        if k in obj:
            raise Malformed("a key appears twice")
        obj[k] = v
    return obj


def _no_constant(name):
    raise Malformed("NaN or Infinity is not JSON")


def _finite(text):
    x = float(text)
    if not math.isfinite(x):            # 1e999 parses to inf without ever reaching parse_constant
        raise Malformed("a number out of range")
    return x


def _no_lone_surrogates(obj):
    """"\\ud800" is valid JSON text but no valid string: refuse it here rather than at some later encode."""
    stack = [obj]
    while stack:
        x = stack.pop()
        if isinstance(x, dict):
            stack.extend(x.keys())
            stack.extend(x.values())
        elif isinstance(x, list):
            stack.extend(x)
        elif isinstance(x, str):
            try:
                x.encode("utf-8")
            except UnicodeEncodeError:
                raise Malformed("a lone surrogate") from None


def strict_loads(data):
    """Parse JSON bytes strictly: UTF-8 without a BOM, no repeated keys (at any depth), no NaN or Infinity, no number
    that overflows to infinity, no lone surrogate. Anything else raises Malformed; the message never quotes the input."""
    if not isinstance(data, (bytes, bytearray)):
        raise Malformed("not bytes")
    if bytes(data[:3]) == b"\xef\xbb\xbf":
        raise Malformed("a byte order mark")
    try:
        obj = json.loads(bytes(data).decode("utf-8"), object_pairs_hook=_no_repeats, parse_constant=_no_constant,
                         parse_float=_finite)
    except Malformed:
        raise
    except (ValueError, RecursionError) as e:   # JSONDecodeError, UnicodeDecodeError, too many digits, too deep
        raise Malformed("not strict JSON (" + type(e).__name__ + ")") from None
    _no_lone_surrogates(obj)
    return obj


def require(obj, schema):
    """obj must be a dict with exactly the keys of schema, each value of its type(s). A bool is never an int."""
    if not isinstance(obj, dict):
        raise Malformed("not an object")
    if set(obj) != set(schema):
        raise Malformed("wrong keys")
    for key, types in schema.items():
        types = types if isinstance(types, tuple) else (types,)
        v = obj[key]
        if (isinstance(v, bool) and bool not in types) or not isinstance(v, types):
            raise Malformed("wrong type: " + key)
    return obj


def b64u_encode(raw):
    """base64url without padding."""
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")


def b64u_decode(s):
    """base64url without padding, and only the one canonical spelling of the bytes (stray low bits are refused)."""
    if not isinstance(s, str) or not B64U_RE.fullmatch(s) or len(s) % 4 == 1:
        raise Malformed("not base64url")
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (binascii.Error, ValueError):
        raise Malformed("not base64url") from None
    if b64u_encode(raw) != s:
        raise Malformed("not canonical base64url")
    return raw


def valid_nonce(s):
    """True for exactly 32 bytes in canonical base64url (43 characters)."""
    if not isinstance(s, str) or not NONCE_RE.fullmatch(s):
        return False
    try:
        return len(b64u_decode(s)) == 32
    except Malformed:
        return False


def _valid_origin(origin):
    m = ORIGIN_RE.fullmatch(origin) if isinstance(origin, str) else None
    return m is not None and int(m.group("port")) <= 65535


def challenge(action, params, list_sha, nonce, exp, origin):
    """The 32 bytes the phone signs: SHA-256 of `zeus-root/v1\\n` + the canonical JSON of
    {action, exp, list, nonce, origin, params} (contract §8b). Root calls it with its own list hash and its own origin.

    params is {} for every listed action and exactly {"key": <key ref>} for check-key. Every field is checked first."""
    if not isinstance(action, str) or not ID_RE.fullmatch(action):
        raise Malformed("bad action")
    if action == CHECK_KEY:
        ok = (isinstance(params, dict) and set(params) == {"key"} and isinstance(params["key"], str)
              and REF_RE.fullmatch(params["key"]) is not None)
    else:
        ok = isinstance(params, dict) and not params
    if not ok:
        raise Malformed("bad params")
    if not isinstance(list_sha, str) or not HEX64_RE.fullmatch(list_sha):
        raise Malformed("bad list")
    if not valid_nonce(nonce):
        raise Malformed("bad nonce")
    if type(exp) is not int or not 0 < exp <= MAX_SAFE_INT:
        raise Malformed("bad exp")
    if not _valid_origin(origin):
        raise Malformed("bad origin")
    body = canonical({"action": action, "exp": exp, "list": list_sha, "nonce": nonce, "origin": origin,
                      "params": dict(params)})
    return hashlib.sha256(b"zeus-root/v1\n" + body).digest()


def key_ref(cred_id):
    """The first 16 lowercase hex digits of SHA-256 over the RAW credential id bytes (never its base64url text).
    The only form of a key id that may appear in results, logs, labels or the journal."""
    if not isinstance(cred_id, (bytes, bytearray)):
        raise TypeError("key_ref takes the raw credential id bytes")
    return hashlib.sha256(bytes(cred_id)).hexdigest()[:16]


def run_id(nonce):
    """D4: "r" + the first 20 hex of SHA-256(`zeus-root/run\\n` + nonce). The gate computes the same for `finish`."""
    if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
        raise Malformed("bad nonce")
    return "r" + hashlib.sha256(b"zeus-root/run\n" + nonce.encode("ascii")).hexdigest()[:20]
