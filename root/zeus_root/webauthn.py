"""Verify the phone's WebAuthn assertion for one record (contract §8b, design §16.4 step 6, W3C WebAuthn Level 3 §7.2).

`verify()` is pure: it reads no file, clock or command. The runner gives it the record, the registered keys, the
config, the installed list's hash, the time and the key's stored counter, and gets back `Verified` or a `Refusal`
(or `canon.Malformed` for a record without exactly the expected shape, which the runner files as garbage).

The order of the checks is the plan's D10, and it matters:
- the key and the device first, so a record that names no registered key (or another device) is garbage and never
  counts toward the pause;
- then the client data, the RP ID hash and UP+UV, which count toward the pause for a registered key (a look-alike
  page or a lying gate);
- then the signature, and only after it the counter and the BE flag, so junk that names a real key can never make a
  "your key may be copied" buzz (Review Focus 2; the private design's 6.3 lists the counter first);
- then freshness (a late resend with a good signature is not counted), then the list (a yes given before the list
  changed).

Authenticator data is sliced by position, never parsed as CBOR: an assertion's is exactly 37 bytes with AT and ED clear.
`cryptography` is imported only where a signature is checked, so the parsing here runs on any Python.
"""
import hashlib
import hmac
from typing import NamedTuple

from . import canon

AUTH_GET_LEN = 37                      # rpIdHash 32 + flags 1 + signCount 4
FLAG_UP, FLAG_UV, FLAG_BE, FLAG_BS, FLAG_AT, FLAG_ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80
ES256, RS256 = -7, -257
RSA_MIN_BITS = 2048
EXP_AHEAD_S = 180                      # a challenge expires at most 180 s after it was issued
RECORD_TYPES = ("run", "check-key")
RECORD_SCHEMA = {"type": str, "action": str, "params": dict, "list": str, "nonce": str, "exp": int,
                 "credentialId": str, "clientDataJSON": str, "authenticatorData": str, "signature": str,
                 "userHandle": (str, type(None)), "seen": dict}
SEEN_SCHEMA = {"login": str, "node": str, "node_id": str, "ip": str}

# reason -> (class, attention kind or None, root's one plain sentence for the key page). Classes and kinds: contract 8e, 8f.
REFUSALS = {
    "unknown-key": ("garbage", None, "This key is not registered here. Nothing ran."),
    "revoked-key": ("garbage", None, "This key was removed. Nothing ran."),
    "wrong-action-key": ("garbage", None, "This key belongs to another action. Nothing ran."),
    "wrong-user": ("garbage", None, "This key's user does not match its registration. Nothing ran."),
    "wrong-device": ("garbage", None, "This key is registered to another device. Nothing ran."),
    "challenge": ("counted", "refused-counted", "What the phone signed is not what Zeus asked for. Nothing ran."),
    "origin": ("counted", "refused-counted", "The request did not come from the key page. Nothing ran."),
    "framed": ("counted", "refused-counted", "The key page was opened inside another page. Nothing ran."),
    "uv": ("counted", "refused-counted", "The phone did not confirm your fingerprint or screen lock. Nothing ran."),
    "signature": ("counted", "refused-counted", "The signature did not check out. Nothing ran."),
    "counter": ("other", "copied-key", "This key's counter did not go up, so it may have been copied. Nothing ran."),
    "be-changed": ("other", "copied-key",
                   "This key's backup setting changed since it was registered, so it may have been copied. Nothing ran."),
    "expired": ("not-counted", None, "The request was too old or dated ahead; try again. Nothing ran."),
    "list-changed": ("other", "list-changed", "The list of actions changed after you tapped; open it again. Nothing ran."),
}


class Refusal(Exception):
    """A record root will not run: the reason, its class (contract 8e), the attention kind it writes (8f) or None,
    and root's one plain sentence for the key page."""

    def __init__(self, reason, cls, attention=None, sentence=""):
        super().__init__(reason)
        self.reason, self.cls, self.attention, self.sentence = reason, cls, attention, sentence


def _refuse(reason):
    cls, attention, sentence = REFUSALS[reason]
    return Refusal(reason, cls, attention, sentence)


class AuthData(NamedTuple):
    rp_id_hash: bytes
    up: bool
    uv: bool
    be: bool
    bs: bool
    count: int


class Verified(NamedTuple):
    key: object            # config.Key
    auth: AuthData
    challenge: bytes


def parse_auth_get(raw):
    """An assertion's authenticator data: exactly 37 bytes, AT and ED clear, and BS only with BE (L3 §6.1.3)."""
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != AUTH_GET_LEN:
        raise canon.Malformed("authenticator data is not 37 bytes")
    flags = raw[32]
    if flags & (FLAG_AT | FLAG_ED):
        raise canon.Malformed("authenticator data carries attested data or extensions")
    if flags & FLAG_BS and not flags & FLAG_BE:
        raise canon.Malformed("backup state set without backup eligibility")
    return AuthData(rp_id_hash=bytes(raw[:32]), up=bool(flags & FLAG_UP), uv=bool(flags & FLAG_UV),
                    be=bool(flags & FLAG_BE), bs=bool(flags & FLAG_BS), count=int.from_bytes(raw[33:37], "big"))


def parse_client(raw, typ):
    """clientDataJSON as strict JSON: an object whose type, challenge and origin are strings and whose crossOrigin,
    when present, is a bool (browsers may add other keys; they are ignored, never compared to a template).
    A type other than `typ` is a `challenge` refusal: what was signed is not this ceremony."""
    obj = canon.strict_loads(raw)
    if not isinstance(obj, dict):
        raise canon.Malformed("client data is not an object")
    for field in ("type", "challenge", "origin"):
        if not isinstance(obj.get(field), str):
            raise canon.Malformed("client data: " + field)
    if "crossOrigin" in obj and not isinstance(obj["crossOrigin"], bool):
        raise canon.Malformed("client data: crossOrigin")
    if obj["type"] != typ:
        raise _refuse("challenge")
    return obj


def verify_sig(alg, spki, auth, client, sig):
    """True when sig is the key's signature over authenticatorData || SHA-256(clientDataJSON).
    ES256: a P-256 key and a DER signature. RS256: PKCS#1 v1.5 with SHA-256 and a modulus of at least 2048 bits.
    Any other algorithm, a key of another type or size, or an unreadable key is False."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives.serialization import load_der_public_key
    try:
        pub = load_der_public_key(bytes(spki))
    except (ValueError, TypeError):
        return False
    msg = bytes(auth) + hashlib.sha256(bytes(client)).digest()
    try:
        if alg == ES256:
            if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256R1):
                return False
            pub.verify(bytes(sig), msg, ec.ECDSA(hashes.SHA256()))
        elif alg == RS256:
            if not isinstance(pub, rsa.RSAPublicKey) or pub.key_size < RSA_MIN_BITS:
                return False
            pub.verify(bytes(sig), msg, padding.PKCS1v15(), hashes.SHA256())
        else:
            return False
    except (InvalidSignature, ValueError):
        return False
    return True


def _shape(rec):
    """The record's exact key set and types (D1), and the parts that must decode. Raises canon.Malformed."""
    canon.require(rec, RECORD_SCHEMA)
    canon.require(rec["seen"], SEEN_SCHEMA)
    if rec["type"] not in RECORD_TYPES or (rec["type"] == "check-key") != (rec["action"] == canon.CHECK_KEY):
        raise canon.Malformed("type and action do not match")
    raw = {f: canon.b64u_decode(rec[f]) for f in ("credentialId", "clientDataJSON", "authenticatorData", "signature")}
    if not raw["credentialId"] or not raw["signature"]:
        raise canon.Malformed("an empty credential id or signature")
    raw["userHandle"] = None if rec["userHandle"] is None else canon.b64u_decode(rec["userHandle"])
    return raw


def verify(rec, keys, cfg, list_sha, now, counter):
    """Check one run or check-key record (D10). keys: registered keys by ref; cfg: config.Config; list_sha: the
    installed actions.json's sha256 hex; now: unix seconds; counter: the key's stored sign counter (None before its
    first use, read as 0). Returns Verified; raises Refusal, or canon.Malformed for a record of the wrong shape."""
    raw = _shape(rec)
    auth = parse_auth_get(raw["authenticatorData"])
    # root's own challenge, from the record's fields and root's own origin (never one from the record)
    want = canon.challenge(rec["action"], rec["params"], rec["list"], rec["nonce"], rec["exp"], cfg.origin)

    # 1. the key: registered, not revoked, for this action (or check-key naming this very key), this user handle
    key = keys.get(canon.key_ref(raw["credentialId"]))
    if key is None or not hmac.compare_digest(key.id, raw["credentialId"]):
        raise _refuse("unknown-key")
    if key.revoked is not None:
        raise _refuse("revoked-key")
    if rec["action"] == canon.CHECK_KEY:
        if rec["params"].get("key") != key.ref:
            raise _refuse("wrong-action-key")
    elif key.action != rec["action"]:
        raise _refuse("wrong-action-key")
    if raw["userHandle"] is None or not hmac.compare_digest(key.user, raw["userHandle"]):
        raise _refuse("wrong-user")
    # 2. the device the gate saw (its word, not proof; the stable node id, never the name)
    if rec["seen"]["node_id"] != key.node_id:
        raise _refuse("wrong-device")
    # 3. the client data: type, exact origin, not framed, root's challenge
    client = parse_client(raw["clientDataJSON"], "webauthn.get")
    if client["origin"] != cfg.origin:
        raise _refuse("origin")
    if client.get("crossOrigin") is True or "topOrigin" in client:
        raise _refuse("framed")
    if client["challenge"] != canon.b64u_encode(want):
        raise _refuse("challenge")
    # 4. the authenticator data: RP ID hash, user present and verified
    if not hmac.compare_digest(auth.rp_id_hash, hashlib.sha256(cfg.rp_id.encode("ascii")).digest()):
        raise _refuse("origin")
    if not (auth.up and auth.uv):
        raise _refuse("uv")
    # 5. the signature, before anything that could say "copied"
    if not verify_sig(key.alg, key.spki, raw["authenticatorData"], raw["clientDataJSON"], raw["signature"]):
        raise _refuse("signature")
    # 6. the counter (either one not zero and the new one not larger) and the BE flag
    stored = 0 if counter is None else counter
    if (auth.count or stored) and auth.count <= stored:
        raise _refuse("counter")
    if auth.be != key.be:
        raise _refuse("be-changed")
    # 7. freshness: now < exp <= now + 180 (the nonce is spent by the runner, right after this)
    if not now < rec["exp"] <= now + EXP_AHEAD_S:
        raise _refuse("expired")
    # 8. the list the owner saw is the list installed now
    if rec["list"] != list_sha:
        raise _refuse("list-changed")
    return Verified(key=key, auth=auth, challenge=want)
