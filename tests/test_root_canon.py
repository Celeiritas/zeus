"""zeus-root: canonical JSON, strict parsing, base64url, the challenge, key refs and run ids (docs/contract.md §8b)."""
import hashlib
import unittest

from tests import root_support  # noqa: F401  (puts root/ on sys.path)
from zeus_root import canon
from zeus_root.canon import Malformed

NONCE = "A" * 43
ORIGIN = "https://h.example:4443"


class CanonicalTest(unittest.TestCase):
    def test_canonical_is_sorted_compact_ascii(self):
        self.assertEqual(canon.canonical({"b": 1, "a": "é"}), b'{"a":"\\u00e9","b":1}')

    def test_canonical_refuses_nan(self):
        with self.assertRaises(ValueError):
            canon.canonical({"a": float("nan")})


class StrictTest(unittest.TestCase):
    def test_strict_refuses_duplicate_keys(self):
        with self.assertRaises(Malformed):
            canon.strict_loads(b'{"a":1,"a":2}')
        with self.assertRaises(Malformed):
            canon.strict_loads(b'{"x":{"a":1,"a":1}}')

    def test_strict_refuses_bom_nan_and_bad_utf8(self):
        for raw in (b'\xef\xbb\xbf{}', b'{"a":NaN}', b'{"a":Infinity}', b'{"a":"\xff"}', b'{"a":', b''):
            with self.subTest(raw=raw), self.assertRaises(Malformed):
                canon.strict_loads(raw)

    def test_strict_accepts_plain_object(self):
        self.assertEqual(canon.strict_loads(b'{"a":[1,"x",null,true]}'), {"a": [1, "x", None, True]})

    def test_require_exact_keys_and_types(self):
        schema = {"n": int, "s": str, "o": (str, type(None))}
        self.assertEqual(canon.require({"n": 1, "s": "x", "o": None}, schema), {"n": 1, "s": "x", "o": None})
        for bad in ({"n": 1, "s": "x"}, {"n": 1, "s": "x", "o": None, "extra": 1}, {"n": True, "s": "x", "o": None},
                    {"n": 1.0, "s": "x", "o": None}, {"n": 1, "s": 2, "o": None}, ["n"]):
            with self.subTest(bad=bad), self.assertRaises(Malformed):
                canon.require(bad, schema)


class B64Test(unittest.TestCase):
    def test_roundtrip_without_padding(self):
        for raw in (b"", b"\x00", b"\xff\xfe", bytes(range(32))):
            s = canon.b64u_encode(raw)
            self.assertNotIn("=", s)
            self.assertEqual(canon.b64u_decode(s), raw)

    def test_refuses_padding_other_alphabet_and_non_canonical(self):
        for s in ("AA==", "A+B/", "A", "AB C", "AB\n", "AB", "é"):
            if s == "AB":
                s = "AB"  # "AB" decodes to one byte with stray low bits: not canonical (canonical is "AA")
            with self.subTest(s=s), self.assertRaises(Malformed):
                canon.b64u_decode(s)
        with self.assertRaises(Malformed):
            canon.b64u_decode(None)


class ChallengeTest(unittest.TestCase):
    def test_challenge_known_answer(self):
        body = ('{"action":"close-media-lan","exp":1794000000,"list":"' + "0" * 64 + '","nonce":"' + NONCE
                + '","origin":"' + ORIGIN + '","params":{}}').encode("ascii")
        want = hashlib.sha256(b"zeus-root/v1\n" + body).digest()
        got = canon.challenge("close-media-lan", {}, "0" * 64, NONCE, 1794000000, ORIGIN)
        self.assertEqual(got, want)
        self.assertEqual(len(got), 32)

    def test_check_key_params(self):
        ref = "0123456789abcdef"
        body = ('{"action":"check-key","exp":5,"list":"' + "a" * 64 + '","nonce":"' + NONCE + '","origin":"'
                + ORIGIN + '","params":{"key":"' + ref + '"}}').encode("ascii")
        self.assertEqual(canon.challenge("check-key", {"key": ref}, "a" * 64, NONCE, 5, ORIGIN),
                         hashlib.sha256(b"zeus-root/v1\n" + body).digest())

    def test_non_ascii_and_bad_formats_refused(self):
        """Review Focus 4: anything that could make Python's and the page's canonical JSON differ is refused."""
        good = dict(action="close-media-lan", params={}, list_sha="0" * 64, nonce=NONCE, exp=1794000000, origin=ORIGIN)
        bad = [dict(action="Close"), dict(action="close_media"), dict(action="clöse"), dict(action=""),
               dict(action="a" * 41), dict(params={"key": "é"}), dict(params={"key": 1}), dict(params=[]),
               dict(list_sha="0" * 63), dict(list_sha="A" * 64), dict(nonce="A" * 42), dict(nonce="A" * 44),
               dict(nonce="A" * 42 + "="), dict(nonce="A" * 42 + "B"), dict(exp=True), dict(exp=1.5),
               dict(exp="1"), dict(origin="https://hé.example")]
        for change in bad:
            with self.subTest(change=change), self.assertRaises(Malformed):
                canon.challenge(**dict(good, **change))

    def test_valid_nonce(self):
        self.assertTrue(canon.valid_nonce(canon.b64u_encode(bytes(32))))
        self.assertFalse(canon.valid_nonce(canon.b64u_encode(bytes(31))))
        self.assertFalse(canon.valid_nonce("A" * 42 + "B"))  # 43 chars but stray low bits
        self.assertFalse(canon.valid_nonce(None))


class RefTest(unittest.TestCase):
    def test_key_ref_is_16_hex_of_raw_bytes(self):
        self.assertEqual(canon.key_ref(b"\x01\x02"), hashlib.sha256(b"\x01\x02").hexdigest()[:16])

    def test_run_id_from_nonce(self):
        a = canon.run_id(NONCE)
        self.assertRegex(a, r"\Ar[0-9a-f]{20}\Z")
        self.assertEqual(a, "r" + hashlib.sha256(b"zeus-root/run\n" + NONCE.encode()).hexdigest()[:20])
        self.assertEqual(a, canon.run_id(NONCE))
        self.assertNotEqual(a, canon.run_id("B" * 43))


if __name__ == "__main__":
    unittest.main()
