"""zeus-root: canonical JSON, strict parsing, base64url, the challenge, key refs and run ids (docs/contract.md §8b)."""
import hashlib
import os
import struct
import unittest

from tests import root_support  # noqa: F401  (puts root/ on sys.path)
from zeus_root import canon
from zeus_root.canon import Malformed

NONCE = "A" * 43
ORIGIN = "https://h.example:4443"


def _tzif(version=b"2", footer=b"\nEST5EDT,M3.2.0,M11.1.0\n"):
    """A TZif zone file made by hand (RFC 8536): one zone type, EST at -05:00, no transitions. Version 2 and later
    repeat the block and end with a footer (a POSIX TZ string between newlines); version 1 (b"\\0") has neither."""
    def block(v):
        counts = struct.pack(">6l", 0, 0, 0, 0, 1, 4)    # isut, isstd, leap, time, type, char counts
        return b"TZif" + v + b"\0" * 15 + counts + struct.pack(">lbB", -18000, 0, 0) + b"EST\0"
    if version == b"\0":
        return block(version)
    return block(version) + block(version) + footer


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

    def test_strict_refuses_what_plain_json_accepts(self):
        """json.loads alone takes a lone surrogate and turns 1e999 into infinity; too deep a nest is refused too."""
        for raw in (b'{"a":"\\ud800"}', b'{"\\udc00":1}', b'{"a":1e999}', b'{"a":-1e999}', b'[' * 5000 + b']' * 5000,
                    bytearray(b'{"a":1,"a":1}'), "{}"):
            with self.subTest(raw=raw[:20]), self.assertRaises(Malformed):
                canon.strict_loads(raw)

    def test_depth_limit_is_explicit(self):
        """The limit is root's own, not whatever this Python's parser takes (3.14 parsed 5000 levels in CI)."""
        canon.strict_loads(b'[' * 32 + b']' * 32)
        canon.strict_loads(b'{"a":' * 31 + b'{}' + b'}' * 31)
        for raw in (b'[' * 33 + b']' * 33, b'{"a":' * 32 + b'{}' + b'}' * 32, b'[' * 100 + b']' * 100):
            with self.subTest(len=len(raw)), self.assertRaises(Malformed):
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

    def test_params_belong_to_the_action(self):
        """{} for a listed action, exactly {"key": <ref>} for check-key (contract §8b); nothing in between."""
        good = dict(list_sha="0" * 64, nonce=NONCE, exp=5, origin=ORIGIN)
        for action, params in (("check-key", {}), ("check-key", {"key": "0123456789ABCDEF"}),
                               ("check-key", {"key": "0123456789abcde"}), ("check-key", {"key": "0" * 16, "x": 1}),
                               ("close-media-lan", {"key": "0" * 16})):
            with self.subTest(action=action, params=params), self.assertRaises(Malformed):
                canon.challenge(action, params, **good)

    def test_origin_is_exact_https_with_port(self):
        good = dict(action="close-media-lan", params={}, list_sha="0" * 64, nonce=NONCE, exp=5)
        for origin in ("https://h.example", "http://h.example:4443", "https://H.example:4443", "https://h.example:0",
                       "https://h.example:65536", "https://h.example:04443", "https://h.example:4443/",
                       "https://h.example:4443\n", "https://-h.example:4443", "https://h..example:4443", None):
            with self.subTest(origin=origin), self.assertRaises(Malformed):
                canon.challenge(origin=origin, **good)
        self.assertEqual(len(canon.challenge(origin="https://h.example:65535", **good)), 32)

    def test_exp_bounds(self):
        good = dict(action="close-media-lan", params={}, list_sha="0" * 64, nonce=NONCE, origin=ORIGIN)
        for exp in (0, -1, 2 ** 53):
            with self.subTest(exp=exp), self.assertRaises(Malformed):
                canon.challenge(exp=exp, **good)
        self.assertEqual(len(canon.challenge(exp=2 ** 53 - 1, **good)), 32)

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

    def test_refs_refuse_the_wrong_input(self):
        """A key ref over the base64url text instead of the raw bytes would be a different, wrong ref."""
        with self.assertRaises(TypeError):
            canon.key_ref(canon.b64u_encode(b"\x01\x02"))
        for nonce in ("A" * 42, "A" * 43 + "\n", None):
            with self.subTest(nonce=nonce), self.assertRaises(Malformed):
                canon.run_id(nonce)


class CtxTest(unittest.TestCase):
    def test_iso_seconds_with_offset(self):
        import tempfile
        from zeus_root.ctx import iso
        with tempfile.TemporaryDirectory() as tmp:
            ctx = root_support.make_ctx(tmp)
            self.assertEqual(iso(ctx, 1794000000.9), "2026-11-06T21:20:00+00:00")
            self.assertTrue((ctx.run / "inbox").is_dir())

    def test_system_uses_constants_only(self):
        """Ctx.system() takes no path or id from the environment, and refuses a missing or root gate account."""
        root_support.need("linux")
        import grp
        import pwd
        from unittest import mock
        from zeus_root.ctx import Ctx, run_command
        from datetime import datetime, timedelta, timezone
        from zeus_root.ctx import server_zone
        junk = {k: "/tmp/elsewhere" for k in ("HOME", "ZEUS_DATA", "ZEUS_ROOT_ETC", "XDG_RUNTIME_DIR", "PYTHONPATH")}
        junk["TZ"] = "UTC-14"
        user = mock.Mock(pw_uid=990)
        group = mock.Mock(gr_gid=991)
        with mock.patch.dict("os.environ", junk), mock.patch.object(pwd, "getpwnam", return_value=user), \
                mock.patch.object(grp, "getgrnam", return_value=group):
            ctx = Ctx.system()
        # the zone comes from /etc/localtime, never from TZ (re-check R3)
        moment = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
        self.assertEqual(moment.astimezone(ctx.tz).utcoffset(), moment.astimezone(server_zone()).utcoffset())
        self.assertNotEqual(moment.astimezone(ctx.tz).utcoffset(), timedelta(hours=14))
        self.assertEqual([str(p) for p in (ctx.etc, ctx.run, ctx.lib, ctx.log, ctx.libexec, ctx.proc)],
                         ["/etc/zeus-root", "/run/zeus-root", "/var/lib/zeus-root", "/var/log/zeus-root",
                          "/usr/local/libexec/zeus-root", "/proc"])
        self.assertEqual((ctx.root_uid, ctx.gate_uid, ctx.gate_gid), (0, 990, 991))
        self.assertIs(ctx.cmd, run_command)
        self.assertEqual((ctx.script_limit_s, ctx.undo_limit_s, ctx.lock_wait_s), (120, 120, 30))
        with mock.patch.object(pwd, "getpwnam", side_effect=KeyError("zeus-root-gate")), \
                self.assertRaisesRegex(RuntimeError, "not installed"):
            Ctx.system()
        with mock.patch.object(pwd, "getpwnam", return_value=mock.Mock(pw_uid=0)), \
                mock.patch.object(grp, "getgrnam", return_value=group), self.assertRaisesRegex(RuntimeError, "root"):
            Ctx.system()

    def zone(self, data):
        """server_zone() of a file holding data (None: no file), in a thread with a 10 s limit so that a loop fails
        the test instead of hanging the run."""
        import tempfile
        import threading
        from zeus_root.ctx import server_zone
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "localtime")
            if data is not None:
                with open(path, "wb") as f:
                    f.write(data)
            got = []
            t = threading.Thread(target=lambda: got.append(server_zone(path)), daemon=True)
            t.start()
            t.join(10)
            self.assertFalse(t.is_alive(), "server_zone hung")
        self.assertEqual(len(got), 1)
        return got[0]

    def test_server_zone_reads_the_file_not_tz(self):
        """A real zone file gives its offsets, summer time included, whatever TZ says (TZ=UTC-14 means +14:00)."""
        from datetime import timedelta
        from unittest import mock
        from zeus_root.ctx import iso
        with mock.patch.dict("os.environ", {"TZ": "UTC-14"}):
            z = self.zone(_tzif())
        ctx = mock.Mock(tz=z)
        self.assertEqual(iso(ctx, 1794000000), "2026-11-06T16:20:00-05:00")
        self.assertEqual(iso(ctx, 1782000000), "2026-06-20T20:00:00-04:00")
        self.assertEqual(self.zone(_tzif(version=b"\0")).utcoffset(None), timedelta(hours=-5))

    def test_server_zone_damaged_files_give_utc(self):
        """Re-check R2: a version 2 file whose footer has no closing newline made CPython's zoneinfo loop for ever, and
        some cut files raised struct.error or AssertionError. Every one of these gives UTC, quickly."""
        from datetime import timezone
        good = _tzif()
        cases = {"missing": None, "empty": b"", "bad magic": b"XXXX" + good[4:], "cut at 30": good[:30],
                 "cut at 50": good[:50], "cut in the second block": good[:70],
                 "footer without its closing newline": good[:-1],
                 "only the footer's opening newline": _tzif(footer=b"\n"),
                 "no footer": _tzif(footer=b""), "bad footer": _tzif(footer=b"\n%%%garbage\n"),
                 "negative counts": good[:20] + struct.pack(">6l", -1, -1, -1, -1, -1, -1) + good[44:],
                 "over 64 KB": good + b"\n" * (64 * 1024)}
        for label, data in cases.items():
            with self.subTest(label):
                self.assertIs(self.zone(data), timezone.utc)

    def test_run_command_fixed_environment(self):
        root_support.need("linux")
        from unittest import mock
        from zeus_root.ctx import run_command
        with mock.patch.dict("os.environ", {"LD_PRELOAD": "/tmp/x.so", "PATH": "/tmp/bin:/usr/bin"}):
            r = run_command(["/usr/bin/env"], 10)
        self.assertEqual((r.code, r.out), (0, "PATH=/usr/sbin:/usr/bin\n"))
        self.assertEqual(run_command(["/usr/bin/pwd"], 10).out, "/\n")
        self.assertEqual(run_command(["/usr/bin/sleep", "5"], 0.2).code, 124)
        self.assertEqual(run_command(["/usr/bin/no-such-program-zeus-root"], 5).code, 127)
        for argv in (["env"], [], ["/usr/bin/env", 1]):
            with self.subTest(argv=argv), self.assertRaises(ValueError):
                run_command(argv, 5)


if __name__ == "__main__":
    unittest.main()
