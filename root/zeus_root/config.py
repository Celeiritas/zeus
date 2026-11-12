"""Root's files under ctx.etc (/etc/zeus-root): config.json, the fixed list actions.json (contract §8a), keys.json,
owners.json and labels.json, plus the file rules root applies before every run (design §16.3).

Every loader is strict: strict JSON, exact key sets, exact types, and the rules below. Anything else raises
ConfigInvalid, ListInvalid or KeysInvalid, and the runner refuses: nothing runs on a file root can't read exactly.

File shapes (each an object holding one list): {"actions": [entry, ...]}, {"keys": [key, ...]}, {"owners": [login, ...]},
{"labels": [row, ...]}. In keys.json the secret parts keep their WebAuthn names (credentialId, userId, publicKey, as
base64url), the very names logs.assert_no_ids refuses, so they can't slip into a readable file under another name.
"""
import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Optional

from . import canon, logs, state, webauthn

KINDS = ("check", "change", "put-back")
SCRIPT_RE = re.compile(r"[A-Za-z0-9._-]+\.sh")
HOST_RE = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
GAME_RE = re.compile(r"[A-Za-z0-9._+-]{1,64}")
LOGIN_RE = re.compile(r"[!-~]{1,256}")               # printable ASCII, no spaces (a Tailscale login name)
NODE_ID_RE = re.compile(r"[!-~]{1,128}")             # a Tailscale stable node id: never empty, no spaces
FUNNEL_PORTS = (443, 8443, 10000)                     # the only ports Tailscale Funnel serves on (private design 10.3)
ALGS = (-7, -257)                                     # ES256, RS256


class ConfigInvalid(ValueError):
    pass


class ListInvalid(ValueError):
    pass


class KeysInvalid(ValueError):
    pass


def _load(path, invalid, schema=None):
    """Strict JSON from a root-owned file; a missing file is None."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    try:
        obj = canon.strict_loads(raw)
        if schema is not None:
            canon.require(obj, schema)
    except canon.Malformed as e:
        raise invalid(path.name + ": " + str(e)) from None
    return obj, raw


# --- config.json ---------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    rp_id: str
    origin: str
    keyport: int
    gateport: int
    page_dir: str
    games: tuple
    reach_game_max_s: int = 900


CONFIG_SCHEMA = {"rp_id": str, "origin": str, "keyport": int, "gateport": int, "page_dir": str, "games": list,
                 "reach_game_max_s": int}


def load_config(ctx):
    path = ctx.etc / "config.json"
    try:
        obj = canon.strict_loads(path.read_bytes())
    except FileNotFoundError:
        raise ConfigInvalid("config.json is missing") from None
    except canon.Malformed as e:
        raise ConfigInvalid("config.json: " + str(e)) from None
    if isinstance(obj, dict):
        obj.setdefault("reach_game_max_s", 900)
    try:
        canon.require(obj, CONFIG_SCHEMA)
    except canon.Malformed as e:
        raise ConfigInvalid("config.json: " + str(e)) from None
    rp_id, keyport, gateport = obj["rp_id"], obj["keyport"], obj["gateport"]
    if not HOST_RE.fullmatch(rp_id):
        raise ConfigInvalid("config.json: rp_id is not a lowercase host name")
    if not 1025 <= keyport <= 65535 or keyport in FUNNEL_PORTS:
        raise ConfigInvalid("config.json: keyport is 1025..65535 and not 8443 or 10000 (Funnel could put the key page "
                            "on the internet; and every Serve path on 443 is one origin)")
    if not 0 < gateport < 1024 or gateport == keyport:
        raise ConfigInvalid("config.json: gateport is below 1024 (only systemd can bind it)")
    if obj["origin"] != "https://%s:%d" % (rp_id, keyport):
        raise ConfigInvalid("config.json: origin must be exactly https://<rp_id>:<keyport>")
    page = PurePosixPath(obj["page_dir"])
    if not page.is_absolute() or ".." in page.parts or obj["page_dir"] != str(page):
        raise ConfigInvalid("config.json: page_dir is an absolute path without ..")
    if not all(isinstance(g, str) and GAME_RE.fullmatch(g) for g in obj["games"]):
        raise ConfigInvalid("config.json: games are process names")
    if not 0 <= obj["reach_game_max_s"] <= 3600:
        raise ConfigInvalid("config.json: reach_game_max_s is 0..3600")
    return Config(rp_id=rp_id, origin=obj["origin"], keyport=keyport, gateport=gateport, page_dir=obj["page_dir"],
                  games=tuple(obj["games"]), reach_game_max_s=obj["reach_game_max_s"])


# --- actions.json (the fixed list) ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Action:
    id: str
    kind: str
    title: str
    sheet: str
    button: str
    icon: str
    changes: tuple
    script: str
    sha256: str
    undo: Optional[str]
    game_check: bool
    can_cut_you_off: bool
    ask_hours: int
    max_per_day: int
    min_gap_s: int
    added: str


ACTION_SCHEMA = {"id": str, "kind": str, "title": str, "sheet": str, "button": str, "icon": str, "changes": list,
                 "script": str, "sha256": str, "undo": (str, type(None)), "game_check": bool, "can_cut_you_off": bool,
                 "ask_hours": int, "max_per_day": int, "min_gap_s": int, "added": str}


def _action(e):
    try:
        canon.require(e, ACTION_SCHEMA)
    except canon.Malformed as err:
        raise ListInvalid("an entry: " + str(err)) from None
    name = e["id"]
    if not canon.ID_RE.fullmatch(name) or name == canon.CHECK_KEY:
        raise ListInvalid("an entry's id is not a-z, 0-9 and - (1-40), or is check-key")
    if e["kind"] not in KINDS:
        raise ListInvalid(name + ": kind is check, change or put-back")
    for field in ("title", "sheet", "button", "icon", "added"):
        if not e[field].strip():
            raise ListInvalid(name + ": " + field + " is empty")
    for row in e["changes"]:
        if not (isinstance(row, list) and len(row) == 3 and all(isinstance(x, str) for x in row)):
            raise ListInvalid(name + ": a changes row is [what, before, after]")
    if not SCRIPT_RE.fullmatch(e["script"]):
        raise ListInvalid(name + ": script is one file name ending in .sh")
    if not canon.HEX64_RE.fullmatch(e["sha256"]):
        raise ListInvalid(name + ": sha256 is 64 lowercase hex")
    if e["undo"] is None and e["can_cut_you_off"]:
        raise ListInvalid(name + ": undo null is accepted only with can_cut_you_off false")
    if e["kind"] == "check" and (e["undo"] is not None or e["can_cut_you_off"]):
        raise ListInvalid(name + ": a check has no undo and can't cut you off")
    if not 1 <= e["max_per_day"] <= 100:
        raise ListInvalid(name + ": max_per_day is 1..100")
    if e["min_gap_s"] < 0:
        raise ListInvalid(name + ": min_gap_s is 0 or more")
    return Action(**dict(e, changes=tuple(tuple(r) for r in e["changes"])))


def load_actions(ctx):
    """(actions by id, the list's sha256: hex of the file's exact bytes, which the key page's list.json shares)."""
    top, raw = _load(ctx.etc / "actions.json", ListInvalid, {"actions": list})
    if top is None:
        raise ListInvalid("actions.json is missing")
    actions = {}
    for e in top["actions"]:
        a = _action(e)
        if a.id in actions:
            raise ListInvalid(a.id + ": the id appears twice")
        actions[a.id] = a
    for a in actions.values():
        if a.undo is not None and (a.undo == a.id or a.undo not in actions or actions[a.undo].undo != a.id):
            raise ListInvalid(a.id + ": undo " + a.undo + " is not its pair (each must name the other)")
    return actions, hashlib.sha256(raw).hexdigest()


# --- keys.json -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Key:
    id: bytes
    ref: str
    action: str
    user: bytes
    node_id: str
    node_name: str
    login: str
    alg: int
    spki: bytes
    be: bool
    claim: str
    words: str
    label: str
    made: str
    revoked: Optional[str]


KEY_SCHEMA = {"credentialId": str, "ref": str, "action": str, "userId": str, "node_id": str, "node_name": str,
              "login": str, "alg": int, "publicKey": str, "be": bool, "claim": str, "words": str, "label": str,
              "made": str, "revoked": (str, type(None))}


def _key(k):
    try:
        canon.require(k, KEY_SCHEMA)
        cid, user, spki = (canon.b64u_decode(k[f]) for f in ("credentialId", "userId", "publicKey"))
    except canon.Malformed as e:
        raise KeysInvalid("a key: " + str(e)) from None
    if not 1 <= len(cid) <= 1023 or not 1 <= len(user) <= 64 or not spki:
        raise KeysInvalid("a key: id 1-1023 bytes, user handle 1-64, a public key")
    if k["ref"] != canon.key_ref(cid):
        raise KeysInvalid("a key: ref does not match its id")
    if not canon.ID_RE.fullmatch(k["action"]) or k["alg"] not in ALGS or not LOGIN_RE.fullmatch(k["login"]):
        raise KeysInvalid("key " + k["ref"] + ": action, alg or login")
    if not NODE_ID_RE.fullmatch(k["node_id"]):
        raise KeysInvalid("key " + k["ref"] + ": node_id is empty or not printable")
    if not webauthn.key_fits(k["alg"], spki):
        raise KeysInvalid("key " + k["ref"] + ": the public key does not load or does not fit its alg")
    return Key(id=cid, ref=k["ref"], action=k["action"], user=user, node_id=k["node_id"], node_name=k["node_name"],
               login=k["login"], alg=k["alg"], spki=spki, be=k["be"], claim=k["claim"], words=k["words"],
               label=k["label"], made=k["made"], revoked=k["revoked"])


def load_keys(ctx):
    """Registered keys by ref ({} before the first registration)."""
    top, _ = _load(ctx.etc / "keys.json", KeysInvalid, {"keys": list})
    keys = {}
    for k in (top or {"keys": []})["keys"]:
        key = _key(k)
        if key.ref in keys:
            raise KeysInvalid("key " + key.ref + " appears twice")
        keys[key.ref] = key
    return keys


def save_keys(ctx, keys):
    rows = [{"credentialId": canon.b64u_encode(k.id), "ref": k.ref, "action": k.action,
             "userId": canon.b64u_encode(k.user), "node_id": k.node_id, "node_name": k.node_name, "login": k.login,
             "alg": k.alg, "publicKey": canon.b64u_encode(k.spki), "be": k.be, "claim": k.claim, "words": k.words,
             "label": k.label, "made": k.made, "revoked": k.revoked}
            for k in sorted(keys.values(), key=lambda k: (k.made, k.ref))]
    for row in rows:
        _key(row)   # never write what load_keys would refuse
    state.write_file(ctx.etc / "keys.json", canon.canonical({"keys": rows}) + b"\n", 0o640,
                     (ctx.root_uid, ctx.gate_gid))


# --- owners.json, labels.json --------------------------------------------------------------------------------------

def load_owners(ctx):
    top, _ = _load(ctx.etc / "owners.json", ConfigInvalid, {"owners": list})
    owners = (top or {"owners": []})["owners"]
    if not all(isinstance(o, str) and LOGIN_RE.fullmatch(o) for o in owners):
        raise ConfigInvalid("owners.json: an owner is a login name")
    return sorted(set(owners))


def save_owners(ctx, owners):
    if not all(isinstance(o, str) and LOGIN_RE.fullmatch(o) for o in owners):
        raise ValueError("an owner is a login name (printable ASCII, no spaces)")
    state.write_file(ctx.etc / "owners.json", canon.canonical({"owners": sorted(set(owners))}) + b"\n", 0o640,
                     (ctx.root_uid, ctx.gate_gid))


def save_labels(ctx, keys, actions):
    """labels.json (root 644, read by Elaia): per key its ref, label, device, action, title, kind, made, revoked; no ids."""
    rows = []
    for k in sorted(keys.values(), key=lambda k: (k.made, k.ref)):
        a = actions.get(k.action)
        rows.append({"ref": k.ref, "label": k.label, "device": k.node_name, "action": k.action,
                     "title": a.title if a else None, "kind": a.kind if a else None, "made": k.made,
                     "revoked": k.revoked})
    doc = {"labels": rows}
    logs.assert_no_ids(doc)
    state.write_file(ctx.etc / "labels.json", canon.canonical(doc) + b"\n", 0o644)


# --- the file rules (design §16.3) ---------------------------------------------------------------------------------

def _rules(ctx):
    """(path, folder?, owner uid, group gid or None, exact mode or None = 'no write for group or others', required)."""
    root, gate, gid = ctx.root_uid, ctx.gate_uid, ctx.gate_gid
    return [(ctx.etc, True, root, None, None, True),
            (ctx.etc / "config.json", False, root, None, 0o644, True),
            (ctx.etc / "actions.json", False, root, None, 0o644, True),
            (ctx.etc / "keys.json", False, root, gid, 0o640, True),
            (ctx.etc / "owners.json", False, root, gid, 0o640, True),
            (ctx.etc / "labels.json", False, root, None, 0o644, False),
            (ctx.run, True, root, None, None, True),
            (ctx.run / "inbox", True, gate, None, 0o700, True),
            (ctx.run / "enrol", True, gate, None, 0o700, True),
            (ctx.run / "work", True, root, None, 0o700, True),
            (ctx.run / "quarantine", True, root, None, 0o700, True),
            (ctx.run / "results", True, root, None, 0o755, True),
            (ctx.lib, True, root, None, 0o700, True),
            (ctx.log, True, root, None, 0o755, True),
            (ctx.libexec, True, root, None, None, True),
            (ctx.libexec / "actions", True, root, None, None, True)]


def _check(path, folder, uid, gid, mode, required):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return [str(path) + ": missing"] if required else []
    if stat.S_ISLNK(st.st_mode):
        return [str(path) + ": is a link"]
    if not (stat.S_ISDIR if folder else stat.S_ISREG)(st.st_mode):
        return [str(path) + ": not a " + ("folder" if folder else "regular file")]
    problems = []
    if st.st_uid != uid:
        problems.append("owner %d, should be %d" % (st.st_uid, uid))
    if gid is not None and st.st_gid != gid:
        problems.append("group %d, should be %d" % (st.st_gid, gid))
    m = stat.S_IMODE(st.st_mode)
    if mode is not None and m != mode:
        problems.append("mode %o, should be %o" % (m, mode))
    if mode is None and m & 0o022:
        problems.append("mode %o is writable by group or others" % m)
    return [str(path) + ": " + "; ".join(problems)] if problems else []


def file_problems(ctx):
    """One line per path that breaks the rules of design §16.3 (owner, group, mode, no links; every script in
    actions/ a regular root-owned file nobody else can write). [] means every rule holds. Linux only."""
    problems = []
    for rule in _rules(ctx):
        problems += _check(*rule)
    try:
        with os.scandir(ctx.libexec / "actions") as it:
            entries = sorted(it, key=lambda e: e.name)
    except (FileNotFoundError, NotADirectoryError):
        entries = []
    for e in entries:
        problems += _check(e.path, False, ctx.root_uid, None, None, True)
    return problems
