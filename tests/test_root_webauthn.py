"""zeus-root: verifying the phone's WebAuthn assertion (root/zeus_root/webauthn.py, contract §8b, plan Task 6).

Three kinds of evidence:
- the W3C WebAuthn Level 3 §16 test vectors (tests/root_fixtures/w3c-l3-vectors.json): the signature check alone,
  because those vectors carry no user verification and sign for https://example.org;
- two real assertions from a Chromium virtual authenticator (chromium-es256.json, chromium-rs256.json): the whole
  verify(), a genuine record sent again, and the proof that the canonical JSON and challenge computed by the
  generator's in-page function (make_chromium_fixtures.mjs, ceremony) equal Python's. That is parity with the
  generator, not yet with the key page: Z4's page.js must reuse that function verbatim, and Z4 re-runs this test
  against page.js;
- forged records: the Chromium record with exactly one thing changed, signed again with a key made in the test, so
  the one change is the only thing wrong (once with an ES256 key, once with an RS256 key).
"""
import copy
import hashlib
import json
import unittest

from tests.root_support import FIXTURES, Spender, make_key, need

UNKNOWN_OID_SPKI = bytes.fromhex("3019300d06092b0601040182370101050003080030050203010001")

from zeus_root import canon
from zeus_root.config import Config


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _cfg(**over):
    doc = _fixture("config.json")
    doc.update(over)
    return Config(rp_id=doc["rp_id"], origin=doc["origin"], keyport=doc["keyport"], gateport=doc["gateport"],
                  page_dir=doc["page_dir"], games=tuple(doc["games"]), reach_game_max_s=doc["reach_game_max_s"])


_SIGNERS = {}


def cached_signer(alg):
    """One Signer per algorithm for the whole run (an RSA key takes a moment to make)."""
    if alg not in _SIGNERS:
        _SIGNERS[alg] = Signer(alg)
    return _SIGNERS[alg]


def _der(tag, body):
    n = len(body)
    head = bytes([n]) if n < 0x80 else bytes([0x80 | ((n.bit_length() + 7) // 8)]) + n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag]) + head + body


def _der_int(v):
    return _der(0x02, v.to_bytes(v.bit_length() // 8 + 1, "big"))


def _rsa_spki(n, e):
    """An RSA SubjectPublicKeyInfo encoded by hand, for exponents cryptography itself refuses to build (1, even)."""
    alg = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010101")) + b"\x05\x00")
    return _der(0x30, alg + _der(0x03, b"\x00" + _der(0x30, _der_int(n) + _der_int(e))))


def chromium_key(fx):
    """The registered key of a Chromium fixture, as root would have stored it."""
    reg = fx["registration"]
    cid = canon.b64u_decode(reg["credentialId"])
    reg_auth = canon.b64u_decode(reg["authenticatorData"])
    return make_key(id=cid, ref=canon.key_ref(cid), user=canon.b64u_decode(reg["userId"]), alg=fx["alg"],
                    spki=canon.b64u_decode(reg["publicKey"]), be=bool(reg_auth[32] & 0x08))


class Signer:
    """A key made in the test: signs authenticatorData || SHA-256(clientDataJSON) like a phone would."""

    def __init__(self, alg=-7):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        self.alg = alg
        self.priv = (ec.generate_private_key(ec.SECP256R1()) if alg == -7
                     else rsa.generate_private_key(public_exponent=65537, key_size=2048))
        self.spki = self.priv.public_key().public_bytes(serialization.Encoding.DER,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo)

    def sign(self, auth, client):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding
        msg = auth + hashlib.sha256(client).digest()
        if self.alg == -7:
            return self.priv.sign(msg, ec.ECDSA(hashes.SHA256()))
        return self.priv.sign(msg, padding.PKCS1v15(), hashes.SHA256())


FLAGS_OK = 0x01 | 0x04 | 0x08 | 0x10    # UP, UV, BE, BS: a synced passkey with the fingerprint


class Base(unittest.TestCase):
    ALG = -7
    FIXTURE = "chromium-es256.json"

    def setUp(self):
        need("cryptography")
        from zeus_root import webauthn
        self.w = webauthn
        self.fx = _fixture(self.FIXTURE)
        self.cfg = _cfg()
        self.now = self.fx["record"]["exp"] - 60
        self.signer = cached_signer(self.ALG)
        reg = self.fx["registration"]
        self.key = make_key(id=canon.b64u_decode(reg["credentialId"]), ref=canon.key_ref(canon.b64u_decode(reg["credentialId"])),
                            user=canon.b64u_decode(reg["userId"]), alg=self.ALG, spki=self.signer.spki, be=True)
        self.spender = None

    def forge(self, client=None, flags=FLAGS_OK, count=7, rp_id="h.example", auth=None, bad_sig=False, **fields):
        """The Chromium record with `fields` changed in the record, `client` changed in the client data, and the
        authenticator data rebuilt from flags, count and rp_id (or given whole as `auth`); signed with the test key,
        then the signature spoiled if bad_sig. The challenge in the client data is the one of the ORIGINAL record."""
        rec = copy.deepcopy(self.fx["record"])
        cd = json.loads(canon.b64u_decode(rec["clientDataJSON"]))
        cd.update(client or {})
        cd = {k: v for k, v in cd.items() if v is not None}
        client_raw = json.dumps(cd, separators=(",", ":")).encode()
        if auth is None:
            auth = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + count.to_bytes(4, "big")
        sig = self.signer.sign(auth, client_raw)
        if bad_sig:
            sig = sig[:-1] + bytes([sig[-1] ^ 1])
        rec.update(clientDataJSON=canon.b64u_encode(client_raw), authenticatorData=canon.b64u_encode(auth),
                   signature=canon.b64u_encode(sig))
        for k, v in fields.items():
            if k == "seen":
                rec["seen"] = dict(rec["seen"], **v)
            else:
                rec[k] = v
        return rec

    def verify(self, rec, keys=None, cfg=None, list_sha=None, now=None, counter=None, spend=None):
        """verify() with a fresh Spender unless one is given; the one used is self.spender afterwards."""
        keys = {self.key.ref: self.key} if keys is None else keys
        self.spender = Spender() if spend is None else spend
        return self.w.verify(rec, keys, cfg or self.cfg, list_sha or self.fx["record"]["list"],
                             self.now if now is None else now, counter, self.spender)

    def refused(self, reason, rec, **kw):
        with self.assertRaises(self.w.Refusal) as caught:
            self.verify(rec, **kw)
        r = caught.exception
        self.assertEqual(r.reason, reason)
        want_cls, want_att = self.w.REFUSALS[reason][:2]
        self.assertEqual((r.cls, r.attention), (want_cls, want_att))
        self.assertTrue(r.sentence)
        return r


class TestVectors(unittest.TestCase):
    def test_w3c_vectors_verify(self):
        """§16.2 (ES256) and §16.10 (RS256): the signature verifies with the public key derived from the vector's own
        private key; one flipped signature byte, or one flipped authenticator-data byte, does not."""
        need("cryptography")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        from zeus_root import webauthn
        doc = _fixture("w3c-l3-vectors.json")
        self.assertIn("software-license-2023", doc["_source"]["licence"])
        self.assertIn("includes material copied from or derived from", doc["_source"]["notice"])
        self.assertEqual(sorted(doc["vectors"]), ["none-es256", "packed-rs256"])
        for name, v in doc["vectors"].items():
            with self.subTest(name):
                if v["alg"] == -7:
                    pub = ec.derive_private_key(int(v["credential_private_key"], 16), ec.SECP256R1()).public_key()
                else:
                    p, q = int(v["private_key_p"], 16), int(v["private_key_q"], 16)
                    pub = rsa.RSAPublicNumbers(65537, p * q).public_key()
                spki = pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
                a = {k: bytes.fromhex(x) for k, x in v["authentication"].items()}
                self.assertTrue(webauthn.verify_sig(v["alg"], spki, a["authenticatorData"], a["clientDataJSON"],
                                                    a["signature"]))
                bad = a["signature"][:-1] + bytes([a["signature"][-1] ^ 1])
                self.assertFalse(webauthn.verify_sig(v["alg"], spki, a["authenticatorData"], a["clientDataJSON"], bad))
                auth2 = a["authenticatorData"][:36] + bytes([a["authenticatorData"][36] ^ 1])
                self.assertFalse(webauthn.verify_sig(v["alg"], spki, auth2, a["clientDataJSON"], a["signature"]))
                parsed = webauthn.parse_auth_get(a["authenticatorData"])
                self.assertEqual(parsed.rp_id_hash, hashlib.sha256(v["rp_id"].encode()).digest())
                cd = webauthn.parse_client(a["clientDataJSON"], "webauthn.get")
                self.assertEqual(canon.b64u_decode(cd["challenge"]), a["challenge"])
                self.assertEqual(cd["origin"], v["origin"])

    def test_chromium_es256_and_rs256_verify(self):
        """Real assertions from Chromium pass the whole verify() inside their window, and the page's canonical JSON and
        challenge are byte for byte Python's."""
        need("cryptography")
        from zeus_root import webauthn
        for name in ("es256", "rs256"):
            with self.subTest(name):
                fx = _fixture("chromium-" + name + ".json")
                reg, rec = fx["registration"], fx["record"]
                fields = {k: rec[k] for k in ("action", "exp", "list", "nonce", "params")}
                fields["origin"] = fx["origin"]
                self.assertEqual(canon.canonical(fields), fx["page"]["canonical"].encode("ascii"))
                want = canon.challenge(rec["action"], rec["params"], rec["list"], rec["nonce"], rec["exp"], fx["origin"])
                self.assertEqual(canon.b64u_encode(want), fx["page"]["challenge"])
                key = chromium_key(fx)
                spend = Spender()
                got = webauthn.verify(rec, {key.ref: key}, _cfg(), rec["list"], rec["exp"] - 60, 1, spend)
                self.assertEqual(got.key, key)
                self.assertEqual(got.challenge, want)
                self.assertTrue(got.auth.up and got.auth.uv and got.auth.be and got.auth.bs)
                self.assertEqual(got.auth.count, 2)
                self.assertEqual((spend.calls, spend.spent), (1, {rec["nonce"]}))
                # the same record against a second, unrelated key of the same shape: the signature fails
                other = make_key(id=key.id, ref=key.ref, user=key.user, alg=fx["alg"],
                                 spki=cached_signer(fx["alg"]).spki, be=key.be)
                spend = Spender()
                with self.assertRaises(webauthn.Refusal) as caught:
                    webauthn.verify(rec, {key.ref: other}, _cfg(), rec["list"], rec["exp"] - 60, 1, spend)
                self.assertEqual(caught.exception.reason, "signature")
                self.assertEqual((spend.calls, spend.spent), (1, {rec["nonce"]}))


class TestReplay(unittest.TestCase):
    """Hostile review finding 1: a genuine record sent again, from an authenticator that counts. After the first
    answer root stored the record's counter (2), so on a resend the counter check alone would say "copied". The
    freshness and spent-nonce checks come first, so a resend is `used-nonce` (or `expired`), not counted, no buzz."""

    def setUp(self):
        need("cryptography")
        from zeus_root import webauthn
        self.w = webauthn

    def each(self):
        for name in ("es256", "rs256"):
            fx = _fixture("chromium-" + name + ".json")
            key = chromium_key(fx)
            yield name, fx["record"], {key.ref: key}

    def refusal(self, rec, keys, now, spend):
        with self.assertRaises(self.w.Refusal) as caught:
            self.w.verify(rec, keys, _cfg(), rec["list"], now, 2, spend)
        return caught.exception

    def test_resend_after_its_answer_is_used_nonce(self):
        for name, rec, keys in self.each():
            with self.subTest(name):
                spend = Spender({rec["nonce"]})
                r = self.refusal(rec, keys, rec["exp"] - 60, spend)
                self.assertEqual((r.reason, r.cls, r.attention), ("used-nonce", "not-counted", None))
                self.assertEqual(spend.calls, 1)

    def test_resend_two_days_later_is_expired(self):
        for name, rec, keys in self.each():
            with self.subTest(name):
                for spent in ((), (rec["nonce"],)):
                    spend = Spender(spent)
                    r = self.refusal(rec, keys, rec["exp"] + 2 * 86400, spend)
                    self.assertEqual((r.reason, r.cls, r.attention), ("expired", "not-counted", None))
                    self.assertEqual((spend.calls, spend.spent), (1, {rec["nonce"]}))

    def test_refused_once_then_intact_is_used_nonce(self):
        """Re-check R1 on the real records: the gate spoils an unsigned field or a byte of the signature, root refuses,
        then the intact record comes inside its window. With a stored counter of 1 the intact record alone would run
        (the control); after the refusal it is `used-nonce`."""
        for name, rec, keys in self.each():
            sig = canon.b64u_decode(rec["signature"])
            self.assertTrue(self.w.verify(rec, keys, _cfg(), rec["list"], rec["exp"] - 60, 1, Spender()))
            for reason, bad in (("wrong-device", dict(rec, seen=dict(rec["seen"], node_id="nOTHERDEVICE"))),
                                ("wrong-user", dict(rec, userHandle=None)),
                                ("signature", dict(rec, signature=canon.b64u_encode(sig[:-1] + bytes([sig[-1] ^ 1]))))):
                with self.subTest(name, spoiled=reason):
                    spend = Spender()
                    with self.assertRaises(self.w.Refusal) as caught:
                        self.w.verify(bad, keys, _cfg(), rec["list"], rec["exp"] - 60, 1, spend)
                    self.assertEqual(caught.exception.reason, reason)
                    with self.assertRaises(self.w.Refusal) as caught:
                        self.w.verify(rec, keys, _cfg(), rec["list"], rec["exp"] - 60, 1, spend)
                    self.assertEqual(caught.exception.reason, "used-nonce")
                    self.assertEqual(spend.calls, 2)

    def test_same_counter_fresh_and_unspent_is_counter(self):
        """The one case that still says "copied": a genuine signature, fresh, a nonce never seen, and a counter that
        did not go up. A record root answered before cannot get here (its nonce is spent)."""
        for name, rec, keys in self.each():
            with self.subTest(name):
                spend = Spender()
                r = self.refusal(rec, keys, rec["exp"] - 60, spend)
                self.assertEqual((r.reason, r.cls, r.attention), ("counter", "other", "copied-key"))
                self.assertEqual(spend.calls, 1)


class TestRefusals(Base):
    def test_forged_baseline_passes(self):
        """The forging helper itself makes a record that passes: every refusal below comes from its one change."""
        got = self.verify(self.forge())
        self.assertEqual(got.auth.count, 7)
        self.assertEqual((self.spender.calls, self.spender.spent), (1, {self.fx["record"]["nonce"]}))

    def test_spend_once_for_every_answer(self):
        """Re-check R1: spend is called exactly once for every record whose nonce has the right shape, whatever the
        answer (each refusal, malformed, accepted), and never for a record whose nonce has the wrong shape."""
        nonce = self.fx["record"]["nonce"]
        exp = self.fx["record"]["exp"]
        refusals = [("unknown-key", self.forge(), {"keys": {}}),
                    ("wrong-device", self.forge(seen={"node_id": "nOTHERDEVICE"}), {}),
                    ("origin", self.forge(client={"origin": "https://h.example:4444"}), {}),
                    ("uv", self.forge(flags=FLAGS_OK & ~0x04), {}),
                    ("signature", self.forge(bad_sig=True), {}),
                    ("expired", self.forge(), {"now": exp}),
                    ("expired", self.forge(), {"now": exp - 181}),
                    ("counter", self.forge(count=7), {"counter": 7}),
                    ("be-changed", self.forge(flags=FLAGS_OK & ~0x18), {}),
                    ("list-changed", self.forge(), {"list_sha": "0" * 64}),
                    ("used-nonce", self.forge(), {"spend": Spender({nonce})})]
        for reason, rec, kw in refusals:
            with self.subTest(reason=reason, **{k: v for k, v in kw.items() if k == "now"}):
                self.refused(reason, rec, **kw)
                self.assertEqual((self.spender.calls, self.spender.spent), (1, {nonce}))
        for change in ({"extra": 1}, {"exp": True}, {"credentialId": "not base64url!"},
                       {"authenticatorData": canon.b64u_encode(b"\x00" * 36)}):
            with self.subTest(malformed=sorted(change)):
                with self.assertRaises(canon.Malformed):
                    self.verify(dict(self.forge(), **change))
                self.assertEqual((self.spender.calls, self.spender.spent), (1, {nonce}))
        self.verify(self.forge())
        self.assertEqual((self.spender.calls, self.spender.spent), (1, {nonce}))
        for bad in ("short", None, 7, "A" * 42 + "B", "A" * 44):
            with self.subTest(nonce=bad):
                with self.assertRaises(canon.Malformed):
                    self.verify(dict(self.forge(), nonce=bad))
                self.assertEqual(self.spender.calls, 0)
        with self.assertRaises(canon.Malformed):
            self.verify(["not", "a", "record"])
        self.assertEqual(self.spender.calls, 0)

    def test_refused_then_sent_again_intact_is_used_nonce(self):
        """Re-check R1: the gate spoils one part of a genuine record A (an unsigned field, a byte of the signature, the
        signed fields, the authenticator data, the shape), root refuses it, and the gate sends A again intact inside its
        180 s. The refusal spent the nonce, so A is `used-nonce` (not counted, no buzz) and never runs, for a key that
        keeps its counter at 0 and for one that counts. The control shows A alone would have run."""
        for count, stored in ((0, None), (7, 5)):
            intact = self.forge(count=count)
            sig = canon.b64u_decode(intact["signature"])
            spoiled = [("wrong-device", dict(intact, seen=dict(intact["seen"], node_id="nOTHERDEVICE"))),
                       ("wrong-user", dict(intact, userHandle=None)),
                       ("unknown-key", dict(intact, credentialId=canon.b64u_encode(b"another key"))),
                       ("signature", dict(intact, signature=canon.b64u_encode(sig[:-1] + bytes([sig[-1] ^ 1])))),
                       ("challenge", dict(intact, exp=intact["exp"] - 1)),
                       ("malformed", dict(intact, authenticatorData=canon.b64u_encode(b"\x00" * 36))),
                       ("malformed", dict(intact, extra=1))]
            self.assertTrue(self.verify(intact, counter=stored))
            for i, (reason, bad) in enumerate(spoiled):
                with self.subTest(count=count, case=i, spoiled=reason):
                    spend = Spender()
                    if reason == "malformed":
                        with self.assertRaises(canon.Malformed):
                            self.verify(bad, counter=stored, spend=spend)
                    else:
                        self.refused(reason, bad, counter=stored, spend=spend)
                    self.assertEqual(spend.spent, {intact["nonce"]})
                    self.refused("used-nonce", intact, counter=stored, spend=spend)
                    self.assertEqual(spend.calls, 2)

    def test_spend_gets_the_exp_only_once_signed(self):
        """0.8.1 re-check finding 2, and finding B of the re-check of its fix: spend gets the record's exp once the
        signature checked out (then it is the exp the phone signed), so a genuine record's nonce outlives it; None for a
        record refused before that point (its exp is only the gate's word) and when the exp is not an int (malformed)."""
        exp = self.fx["record"]["exp"]
        self.verify(self.forge())
        self.assertEqual(self.spender.exps, [exp])
        self.refused("expired", self.forge(), now=exp - 2 * 86400)
        self.assertEqual(self.spender.exps, [exp])
        before = [("wrong-device", self.forge(seen={"node_id": "nOTHERDEVICE"})),
                  ("unknown-key", dict(self.forge(), credentialId=canon.b64u_encode(b"another key"))),
                  ("challenge", self.forge(exp=exp + 86400)),
                  ("signature", self.forge(bad_sig=True))]
        for reason, rec in before:
            with self.subTest(refused=reason):
                self.refused(reason, rec)
                self.assertEqual(self.spender.exps, [None])
        for bad in (True, 1.5, "1794000000"):
            with self.subTest(exp=bad):
                with self.assertRaises(canon.Malformed):
                    self.verify(dict(self.forge(), exp=bad))
                self.assertEqual(self.spender.exps, [None])

    def test_far_exp_refused_then_near_its_exp_is_used_nonce(self):
        """0.8.1 re-check finding 2, with root's own nonce store: a lying gate issues an exp two days away, the owner
        taps, root refuses `expired` and the key page says "Nothing ran". A day of pruning later the gate hands in the
        same intact record 60 s before its exp: it must be `used-nonce`, not run. A day after its exp it is forgotten."""
        import tempfile
        from tests import root_support
        from zeus_root import state
        exp = self.fx["record"]["exp"]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = root_support.make_ctx(tmp)

            def spend(nonce, e):
                return state.spend_nonce(ctx, nonce, e)
            ctx.now.t = exp - 2 * 86400
            self.refused("expired", self.forge(), now=ctx.now.t, spend=spend)
            ctx.now.t = exp - 60
            state.prune_nonces(ctx)
            self.refused("used-nonce", self.forge(), now=ctx.now.t, spend=spend)
            ctx.now.t = exp + 86400 + 1
            state.prune_nonces(ctx)
            self.assertTrue(state.spend_nonce(ctx, self.fx["record"]["nonce"]))

    def test_junk_with_a_far_exp_is_forgotten_after_a_day(self):
        """Re-check of f4e8ad8, finding B: a record refused before its signature checked out keeps its nonce one day,
        whatever exp it names. Kept until that exp, junk with exp 2^53 - 1 (an unknown key, or a real key with the
        challenge for that exp and a bad signature) left a nonce file until 2106, and garbage never pauses or buzzes."""
        import tempfile
        from tests import root_support
        from zeus_root import state
        far = 2 ** 53 - 1
        rec = self.fx["record"]
        far_challenge = canon.challenge(rec["action"], rec["params"], rec["list"], rec["nonce"], far, self.cfg.origin)
        junk = [("unknown-key", dict(self.forge(exp=far), credentialId=canon.b64u_encode(b"another key"))),
                ("signature", self.forge(client={"challenge": canon.b64u_encode(far_challenge)}, exp=far,
                                         bad_sig=True))]
        for reason, bad in junk:
            with self.subTest(refused=reason), tempfile.TemporaryDirectory() as tmp:
                ctx = root_support.make_ctx(tmp)

                def spend(nonce, e):
                    return state.spend_nonce(ctx, nonce, e)
                self.refused(reason, bad, now=ctx.now.t, spend=spend)
                self.assertEqual(len(list((ctx.lib / "nonces").iterdir())), 1)
                ctx.now.t += 86400 + 1
                state.prune_nonces(ctx)
                self.assertEqual(list((ctx.lib / "nonces").iterdir()), [])

    def test_far_exp_seen_spoiled_first_is_left_to_the_key_page(self):
        """Re-check of f4e8ad8, finding A, pinned so the limit stays in sight: root cannot learn the signed exp from a
        copy it refuses before the signature. So a record whose exp is two days away, first sent spoiled (its exp moved
        near or not), keeps its nonce one day, and the intact record sent 60 s before its exp verifies. Only the key
        page's exp check stops this (contract §8b, Z4), and Z5 installs nothing before it. If root ever closes this
        case, this test changes together with the contract."""
        import tempfile
        from tests import root_support
        from zeus_root import state
        exp = self.fx["record"]["exp"]
        then = exp - 2 * 86400
        intact = self.forge()
        spoiled = [("wrong-device", self.forge(seen={"node_id": "nOTHERDEVICE"})),
                   ("wrong-device", self.forge(seen={"node_id": "nOTHERDEVICE"}, exp=then + 100)),
                   ("challenge", self.forge(exp=then + 100))]
        for i, (reason, bad) in enumerate(spoiled):
            with self.subTest(case=i, refused=reason), tempfile.TemporaryDirectory() as tmp:
                ctx = root_support.make_ctx(tmp)

                def spend(nonce, e):
                    return state.spend_nonce(ctx, nonce, e)
                ctx.now.t = then
                self.refused(reason, bad, now=then, spend=spend)
                ctx.now.t = exp - 60
                state.prune_nonces(ctx)
                self.assertTrue(self.verify(intact, now=ctx.now.t, spend=spend))

    def test_far_exp_seen_spoiled_then_intact_while_kept_is_used_near_its_exp(self):
        """Re-check of de22580, finding 1: after a spoiled first copy, the intact record shown while root still keeps
        the nonce is refused (`expired`, its exp two days away) after its signature checked out, and that signed exp
        now keeps the nonce: sent again as it was 60 s before its exp, it is `used-nonce` and does not run."""
        import tempfile
        from tests import root_support
        from zeus_root import state
        exp = self.fx["record"]["exp"]
        then = exp - 2 * 86400
        intact = self.forge()
        for reason, bad in (("wrong-device", self.forge(seen={"node_id": "nOTHERDEVICE"})),
                            ("challenge", self.forge(exp=then + 100))):
            with self.subTest(refused=reason), tempfile.TemporaryDirectory() as tmp:
                ctx = root_support.make_ctx(tmp)

                def spend(nonce, e):
                    return state.spend_nonce(ctx, nonce, e)
                ctx.now.t = then
                self.refused(reason, bad, now=then, spend=spend)
                ctx.now.t = then + 3600
                self.refused("expired", intact, now=ctx.now.t, spend=spend)
                ctx.now.t = exp - 60
                state.prune_nonces(ctx)
                self.refused("used-nonce", intact, now=ctx.now.t, spend=spend)

    def test_a_reused_nonce_refused_inside_its_180_s_keeps_the_intact_record_out(self):
        """Re-check of 1f44350, N1, with root's own nonce store: a lying gate re-issues a nonce root spent almost a day
        ago. A copy refused inside the new record's 180 s (spoiled before its signature, or busy at the lock: the
        runner's own spend, no exp) keeps the nonce a day from that refusal, so after the next start's prune the
        intact record is `used-nonce`, not run."""
        import tempfile
        from tests import root_support
        from zeus_root import state
        exp = self.fx["record"]["exp"]
        nonce = self.fx["record"]["nonce"]
        intact = self.forge()
        for how in ("spoiled", "busy"):
            with self.subTest(how), tempfile.TemporaryDirectory() as tmp:
                ctx = root_support.make_ctx(tmp)

                def spend(n, e):
                    return state.spend_nonce(ctx, n, e)
                ctx.now.t = exp - 170 - 86400 + 30      # an earlier record with this nonce spent it
                self.assertTrue(state.spend_nonce(ctx, nonce))
                ctx.now.t = exp - 170                    # a copy of the new record, refused inside its 180 s
                state.prune_nonces(ctx)
                if how == "spoiled":
                    self.refused("wrong-device", self.forge(seen={"node_id": "nOTHERDEVICE"}), now=ctx.now.t,
                                 spend=spend)
                else:
                    self.assertFalse(state.spend_nonce(ctx, nonce))
                ctx.now.t = exp - 100                    # the next start prunes: the first spend is a day old now
                state.prune_nonces(ctx)
                ctx.now.t = exp - 90
                self.refused("used-nonce", intact, now=ctx.now.t, spend=spend)

    def test_a_forgery_keeps_its_reason_when_the_nonce_is_spent(self):
        """`used-nonce` is decided only after the signature and freshness: a forgery carrying a spent nonce is still a
        counted `signature` refusal, never laundered into a not-counted one."""
        r = self.refused("signature", self.forge(bad_sig=True), spend=Spender({self.fx["record"]["nonce"]}))
        self.assertEqual(r.cls, "counted")
        self.assertEqual(self.spender.calls, 1)

    def test_an_error_from_spend_is_raised_and_spend_runs_once(self):
        """A spend that fails (a full disk) makes verify fail with that error, accepted record or refused one, and is
        never tried a second time."""
        class Failing(Spender):
            def __call__(self, nonce, exp):
                self.calls += 1
                raise OSError("no space left")
        for label, rec, kw in (("accepted", self.forge(), {}), ("signature", self.forge(bad_sig=True), {}),
                               ("counter", self.forge(count=7), {"counter": 7})):
            with self.subTest(label):
                with self.assertRaises(OSError):
                    self.verify(rec, spend=Failing(), **kw)
                self.assertEqual(self.spender.calls, 1)

    def test_used_nonce_comes_before_counter_and_list(self):
        """A spent nonce wins over a counter that did not go up, a BE flag that changed and a list that changed."""
        spent = {self.fx["record"]["nonce"]}
        self.refused("used-nonce", self.forge(count=7), counter=7, spend=Spender(spent))
        self.refused("used-nonce", self.forge(flags=FLAGS_OK & ~0x18), spend=Spender(spent))
        self.refused("used-nonce", self.forge(), list_sha="0" * 64, spend=Spender(spent))

    def test_bs_may_change_when_be_does_not(self):
        """Backup state (BS) may change between uses (a passkey syncs later); only backup eligibility (BE) is fixed."""
        self.assertFalse(self.verify(self.forge(flags=FLAGS_OK & ~0x10)).auth.bs)
        self.assertTrue(self.verify(self.forge(flags=FLAGS_OK)).auth.bs)
        import dataclasses
        k = dataclasses.replace(self.key, be=False)
        got = self.verify(self.forge(flags=0x01 | 0x04), keys={k.ref: k})
        self.assertEqual((got.auth.be, got.auth.bs), (False, False))

    def test_refuse_unknown_key(self):
        self.refused("unknown-key", self.forge(), keys={})
        other = make_key()
        self.refused("unknown-key", self.forge(), keys={other.ref: other})

    def test_refuse_revoked(self):
        import dataclasses
        k = dataclasses.replace(self.key, revoked="2026-11-12T09:00:00+00:00")
        self.refused("revoked-key", self.forge(), keys={k.ref: k})

    def test_refuse_wrong_action_key(self):
        import dataclasses
        k = dataclasses.replace(self.key, action="open-media-lan")
        self.refused("wrong-action-key", self.forge(), keys={k.ref: k})

    def test_check_key_names_that_very_key(self):
        """check-key is accepted with any registered key of the device, only when params.key is that key's ref."""
        import dataclasses
        k = dataclasses.replace(self.key, action="restart-bluetooth")
        rec = self._check_key_record(k.ref)
        self.assertEqual(self.verify(rec, keys={k.ref: k}).key, k)
        self.refused("wrong-action-key", self._check_key_record("0123456789abcdef"), keys={k.ref: k})

    def _check_key_record(self, ref):
        rec = self.fx["record"]
        params = {"key": ref}
        ch = canon.challenge("check-key", params, rec["list"], rec["nonce"], rec["exp"], self.cfg.origin)
        return self.forge(client={"challenge": canon.b64u_encode(ch)}, type="check-key", action="check-key",
                          params=params)

    def test_refuse_wrong_user(self):
        self.refused("wrong-user", self.forge(userHandle=canon.b64u_encode(b"someone else")))
        self.refused("wrong-user", self.forge(userHandle=None))

    def test_refuse_wrong_device(self):
        self.refused("wrong-device", self.forge(seen={"node_id": "nOTHERDEVICE"}))

    def test_refuse_challenge(self):
        self.refused("challenge", self.forge(client={"type": "webauthn.create"}))
        # the gate swapped the nonce after the phone signed: root's recomputation differs
        self.refused("challenge", self.forge(nonce=canon.b64u_encode(bytes(32))))
        self.refused("challenge", self.forge(exp=self.fx["record"]["exp"] - 1))

    def test_refuse_wrong_origin(self):
        self.refused("origin", self.forge(client={"origin": "https://h.example.evil:4443"}))
        self.refused("origin", self.forge(rp_id="evil.example"))

    def test_refuse_wrong_port(self):
        """Same host, another port: the page signed for :4443; a page on :4444, or a root configured for :4444."""
        self.refused("origin", self.forge(client={"origin": "https://h.example:4444"}))
        self.refused("origin", self.forge(), cfg=_cfg(keyport=4444, origin="https://h.example:4444"))

    def test_refuse_framed(self):
        self.refused("framed", self.forge(client={"crossOrigin": True}))
        self.refused("framed", self.forge(client={"topOrigin": "https://h.example:4443"}))

    def test_refuse_uv0(self):
        """The UV bit cleared and the record signed again, so only UV fails; the same for UP."""
        self.refused("uv", self.forge(flags=FLAGS_OK & ~0x04))
        self.refused("uv", self.forge(flags=FLAGS_OK & ~0x01))

    def test_refuse_signature(self):
        self.refused("signature", self.forge(bad_sig=True))
        rec = self.forge()
        rec["signature"] = canon.b64u_encode(b"\x30\x00")
        self.refused("signature", rec)

    def test_refuse_counter_backwards(self):
        self.refused("counter", self.forge(count=7), counter=9)
        self.refused("counter", self.forge(count=7), counter=7)
        self.refused("counter", self.forge(count=0), counter=7)

    def test_counter_zero_zero_allowed(self):
        self.assertEqual(self.verify(self.forge(count=0), counter=0).auth.count, 0)
        self.assertEqual(self.verify(self.forge(count=0), counter=None).auth.count, 0)
        self.assertEqual(self.verify(self.forge(count=8), counter=7).auth.count, 8)
        self.assertEqual(self.verify(self.forge(count=1), counter=0).auth.count, 1)

    def test_refuse_be_flipped(self):
        self.refused("be-changed", self.forge(flags=FLAGS_OK & ~0x18))
        import dataclasses
        k = dataclasses.replace(self.key, be=False)
        self.refused("be-changed", self.forge(), keys={k.ref: k})

    def test_refuse_expired(self):
        exp = self.fx["record"]["exp"]
        self.refused("expired", self.forge(), now=exp)
        self.refused("expired", self.forge(), now=exp + 3600)
        self.assertTrue(self.verify(self.forge(), now=exp - 1))

    def test_refuse_exp_too_far(self):
        exp = self.fx["record"]["exp"]
        self.refused("expired", self.forge(), now=exp - 181)
        self.assertTrue(self.verify(self.forge(), now=exp - 180))

    def test_refuse_list_changed(self):
        self.refused("list-changed", self.forge(), list_sha="0" * 64)

    def test_refuse_oversized_auth_data(self):
        good = hashlib.sha256(b"h.example").digest() + bytes([FLAGS_OK]) + (7).to_bytes(4, "big")
        for auth in (good + b"\x00", good[:-1], hashlib.sha256(b"h.example").digest() + bytes([FLAGS_OK | 0x40]) + good[33:],
                     hashlib.sha256(b"h.example").digest() + bytes([FLAGS_OK | 0x80]) + good[33:],
                     hashlib.sha256(b"h.example").digest() + bytes([0x01 | 0x04 | 0x10]) + good[33:]):
            with self.subTest(len=len(auth), flags=auth[32] if len(auth) > 32 else None):
                with self.assertRaises(canon.Malformed):
                    self.verify(self.forge(auth=auth))

    def test_unsigned_counter_forgery_is_signature_not_copied_key(self):
        """Review Focus 2: junk naming a real key, with a counter or BE flag that would look like a copied key, is a bad
        signature (counted), never `copied-key`: the signature is checked before the counter and the BE flag."""
        r = self.refused("signature", self.forge(count=1, bad_sig=True), counter=5)
        self.assertNotEqual(r.attention, "copied-key")
        self.refused("signature", self.forge(flags=FLAGS_OK & ~0x18, bad_sig=True), counter=5)

    def test_expired_with_bad_signature_is_signature(self):
        """Freshness is checked after the signature, so a late resend is `expired` only when it is genuine."""
        self.refused("signature", self.forge(bad_sig=True), now=self.fx["record"]["exp"] + 10)

    def test_malformed_records(self):
        rec = self.forge()
        for change in ({"extra": 1}, {"type": "reach"}, {"type": "run", "action": "check-key"},
                       {"type": "check-key"}, {"params": {"x": 1}}, {"nonce": "short"}, {"exp": True},
                       {"credentialId": "not base64url!"}, {"seen": {"login": "a", "node": "b", "node_id": "c"}},
                       {"clientDataJSON": canon.b64u_encode(b"[]")}, {"clientDataJSON": canon.b64u_encode(b"{")}):
            with self.subTest(change=sorted(change)):
                bad = dict(rec, **change)
                with self.assertRaises(canon.Malformed):
                    self.verify(bad)
        missing = dict(rec)
        del missing["userHandle"]
        with self.assertRaises(canon.Malformed):
            self.verify(missing)

    def test_refusal_table(self):
        """The classes and attention kinds of plan Task 6 (contract 8e, 8f)."""
        want = {"unknown-key": ("garbage", None), "revoked-key": ("garbage", None),
                "wrong-action-key": ("garbage", None), "wrong-user": ("garbage", None),
                "wrong-device": ("garbage", None), "challenge": ("counted", "refused-counted"),
                "origin": ("counted", "refused-counted"), "framed": ("counted", "refused-counted"),
                "uv": ("counted", "refused-counted"), "signature": ("counted", "refused-counted"),
                "counter": ("other", "copied-key"), "be-changed": ("other", "copied-key"),
                "expired": ("not-counted", None), "used-nonce": ("not-counted", None),
                "list-changed": ("other", "list-changed")}
        self.assertEqual({k: v[:2] for k, v in self.w.REFUSALS.items()}, want)
        from zeus_root import logs
        for reason, (cls, att, sentence) in self.w.REFUSALS.items():
            self.assertIn(cls, logs.CLASSES)
            self.assertTrue(att is None or att in logs.KINDS)
            self.assertTrue(sentence.endswith(".") and sentence.isascii())


class TestRefusalsRS256(TestRefusals):
    """Every refusal above again, with an RS256 key and the Chromium RS256 record (a 2048-bit RSA key, PKCS#1 v1.5)."""
    ALG = -257
    FIXTURE = "chromium-rs256.json"


class TestVerifySig(unittest.TestCase):
    def test_unknown_key_type_is_false_not_an_error(self):
        """A SubjectPublicKeyInfo whose algorithm OID cryptography does not know raises UnsupportedAlgorithm inside;
        verify_sig and key_fits answer False instead of letting it escape (hostile review note 4)."""
        need("cryptography")
        from zeus_root import webauthn
        for alg in (-7, -257):
            with self.subTest(alg=alg):
                self.assertFalse(webauthn.key_fits(alg, UNKNOWN_OID_SPKI))
                self.assertFalse(webauthn.verify_sig(alg, UNKNOWN_OID_SPKI, b"a" * 37, b"{}", b"\x30\x00"))

    def test_key_fits(self):
        """ES256 needs a P-256 key; RS256 an RSA key with an odd modulus of 2048 to 4096 bits and an odd exponent from 3
        to 2^32 - 1. cryptography 50 already refuses an exponent of 1 or an even one at load; webauthn checks it again so
        an older or laxer build cannot let one through. This test checks the answer, whichever layer gives it."""
        need("cryptography")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        from zeus_root import webauthn

        def spki(pub):
            return pub.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        es, rs = cached_signer(-7).spki, cached_signer(-257).spki
        self.assertTrue(webauthn.key_fits(-7, es))
        self.assertTrue(webauthn.key_fits(-257, rs))
        self.assertFalse(webauthn.key_fits(-257, es))
        self.assertFalse(webauthn.key_fits(-7, rs))
        self.assertFalse(webauthn.key_fits(-8, es))
        self.assertFalse(webauthn.key_fits(-7, spki(ec.generate_private_key(ec.SECP384R1()).public_key())))
        n = cached_signer(-257).priv.public_key().public_numbers().n
        self.assertTrue(webauthn.key_fits(-257, spki(rsa.RSAPublicNumbers(3, n).public_key())))
        self.assertEqual(_rsa_spki(n, 65537), rs)
        for e in (1, 2, 65536):
            with self.subTest(e=e):
                self.assertFalse(webauthn.key_fits(-257, _rsa_spki(n, e)))
        # re-check R4: keys cryptography loads but OpenSSL can never verify with, and the bounds themselves
        self.assertTrue(webauthn.key_fits(-257, _rsa_spki(n, 2 ** 32 - 1)))
        self.assertTrue(webauthn.key_fits(-257, _rsa_spki((1 << 4095) + 1, 65537)))     # 4096 bits
        for label, bad_n, e in (("even modulus", n - 1, 65537), ("4097 bits", (1 << 4096) + 1, 65537),
                                ("16401 bits", (1 << 16400) + 1, 65537), ("e = 2^32 + 1", n, 2 ** 32 + 1),
                                ("e over 64 bits", n, 2 ** 65 + 1), ("e above n", n, n + 2)):
            with self.subTest(label):
                self.assertFalse(webauthn.key_fits(-257, _rsa_spki(bad_n, e)))
        small = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
        self.assertFalse(webauthn.key_fits(-257, spki(small)))
        self.assertFalse(webauthn.key_fits(-7, b""))
        self.assertFalse(webauthn.key_fits(-7, b"not a key"))

    def test_wrong_curve_small_rsa_and_mismatch_refused(self):
        need("cryptography")
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec, rsa
        from zeus_root import webauthn
        auth, client = b"a" * 37, b"{}"

        def spki(priv):
            return priv.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
        es = Signer(-7)
        self.assertTrue(webauthn.verify_sig(-7, es.spki, auth, client, es.sign(auth, client)))
        self.assertFalse(webauthn.verify_sig(-257, es.spki, auth, client, es.sign(auth, client)))
        self.assertFalse(webauthn.verify_sig(-8, es.spki, auth, client, es.sign(auth, client)))
        p384 = ec.generate_private_key(ec.SECP384R1())
        from cryptography.hazmat.primitives import hashes
        sig = p384.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        self.assertFalse(webauthn.verify_sig(-7, spki(p384), auth, client, sig))
        from cryptography.hazmat.primitives.asymmetric import padding
        small = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        sig = small.sign(auth + hashlib.sha256(client).digest(), padding.PKCS1v15(), hashes.SHA256())
        self.assertFalse(webauthn.verify_sig(-257, spki(small), auth, client, sig))
        self.assertFalse(webauthn.verify_sig(-7, b"not a key", auth, client, b"\x30\x00"))


if __name__ == "__main__":
    unittest.main()
