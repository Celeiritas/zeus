"""zeus-root: verifying the phone's WebAuthn assertion (root/zeus_root/webauthn.py, contract §8b, plan Task 6).

Three kinds of evidence:
- the W3C WebAuthn Level 3 §16 test vectors (tests/root_fixtures/w3c-l3-vectors.json): the signature check alone,
  because those vectors carry no user verification and sign for https://example.org;
- two real assertions from a Chromium virtual authenticator (chromium-es256.json, chromium-rs256.json): the whole
  verify(), and the proof that the page's canonical JSON and challenge equal Python's;
- forged records: the Chromium record with exactly one thing changed, signed again with a key made in the test, so
  the one change is the only thing wrong.
"""
import copy
import hashlib
import json
import unittest

from tests.root_support import FIXTURES, make_key, need

from zeus_root import canon
from zeus_root.config import Config


def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _cfg(**over):
    doc = _fixture("config.json")
    doc.update(over)
    return Config(rp_id=doc["rp_id"], origin=doc["origin"], keyport=doc["keyport"], gateport=doc["gateport"],
                  page_dir=doc["page_dir"], games=tuple(doc["games"]), reach_game_max_s=doc["reach_game_max_s"])


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
    def setUp(self):
        need("cryptography")
        from zeus_root import webauthn
        self.w = webauthn
        self.fx = _fixture("chromium-es256.json")
        self.cfg = _cfg()
        self.now = self.fx["record"]["exp"] - 60
        self.signer = Signer()
        reg = self.fx["registration"]
        self.key = make_key(id=canon.b64u_decode(reg["credentialId"]), ref=canon.key_ref(canon.b64u_decode(reg["credentialId"])),
                            user=canon.b64u_decode(reg["userId"]), alg=-7, spki=self.signer.spki, be=True)

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

    def verify(self, rec, keys=None, cfg=None, list_sha=None, now=None, counter=None):
        keys = {self.key.ref: self.key} if keys is None else keys
        return self.w.verify(rec, keys, cfg or self.cfg, list_sha or self.fx["record"]["list"],
                             self.now if now is None else now, counter)

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
                cid = canon.b64u_decode(reg["credentialId"])
                reg_auth = canon.b64u_decode(reg["authenticatorData"])
                key = make_key(id=cid, ref=canon.key_ref(cid), user=canon.b64u_decode(reg["userId"]), alg=fx["alg"],
                               spki=canon.b64u_decode(reg["publicKey"]), be=bool(reg_auth[32] & 0x08))
                got = webauthn.verify(rec, {key.ref: key}, _cfg(), rec["list"], rec["exp"] - 60, 1)
                self.assertEqual(got.key, key)
                self.assertEqual(got.challenge, want)
                self.assertTrue(got.auth.up and got.auth.uv and got.auth.be and got.auth.bs)
                self.assertEqual(got.auth.count, 2)
                # the same record against a second, unrelated key of the same shape: the signature fails
                other = make_key(id=cid, ref=key.ref, user=key.user, alg=fx["alg"],
                                 spki=Signer(fx["alg"]).spki, be=key.be)
                with self.assertRaises(webauthn.Refusal) as caught:
                    webauthn.verify(rec, {key.ref: other}, _cfg(), rec["list"], rec["exp"] - 60, 1)
                self.assertEqual(caught.exception.reason, "signature")


class TestRefusals(Base):
    def test_forged_baseline_passes(self):
        """The forging helper itself makes a record that passes: every refusal below comes from its one change."""
        got = self.verify(self.forge())
        self.assertEqual(got.auth.count, 7)

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
                "expired": ("not-counted", None), "list-changed": ("other", "list-changed")}
        self.assertEqual({k: v[:2] for k, v in self.w.REFUSALS.items()}, want)
        from zeus_root import logs
        for reason, (cls, att, sentence) in self.w.REFUSALS.items():
            self.assertIn(cls, logs.CLASSES)
            self.assertTrue(att is None or att in logs.KINDS)
            self.assertTrue(sentence.endswith(".") and sentence.isascii())


class TestVerifySig(unittest.TestCase):
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
