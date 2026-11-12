"""zeus-root: Serve's mapping, the Tailscale operator and TS_PERMIT_CERT_UID, read before every run (root/zeus_root/serve.py,
design §16.4 step 6, private design 5.5 step 4, plan Task 7 and amendment v2). Pure: runs on any system.

The JSON shapes are Tailscale's `ipn.ServeConfig` and `tailscale debug prefs` as written from memory; the Z5 install's
self-test confirms them on the server. Anything that does not read as expected is a refusal (fail closed).
"""
import copy
import json
import tempfile
import unittest

from tests import root_support
from tests.root_support import SERVE_PID, SERVE_PREFS, SERVE_STATUS, TAILSCALED_PID, serve_ok, serve_status
from zeus_root import serve
from zeus_root.config import Config
from zeus_root.ctx import CmdResult


def _doc():
    return json.loads((root_support.FIXTURES / "config.json").read_text(encoding="utf-8"))


def _cfg(doc):
    return Config(rp_id=doc["rp_id"], origin=doc["origin"], keyport=doc["keyport"], gateport=doc["gateport"],
                  page_dir=doc["page_dir"], games=tuple(doc["games"]), reach_game_max_s=doc["reach_game_max_s"])


class MappingTest(unittest.TestCase):
    def setUp(self):
        self.doc = _doc()
        self.cfg = _cfg(self.doc)
        self.hostport = "%s:%d" % (self.doc["rp_id"], self.doc["keyport"])

    def status(self):
        return copy.deepcopy(serve_status(self.doc))

    def test_installed_mapping_passes(self):
        self.assertIsNone(serve.mapping_problem(self.status(), self.cfg))

    def test_other_ports_are_not_root_s_business(self):
        """Argus or Jellyfin served on other ports, even through Funnel, leave the key page as it was."""
        s = self.status()
        s["TCP"]["443"] = {"HTTPS": True}
        s["Web"]["h.example:443"] = {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8080"}}}
        s["AllowFunnel"] = {"h.example:443": True, self.hostport: False}
        self.assertIsNone(serve.mapping_problem(s, self.cfg))
        s["AllowFunnel"] = None
        self.assertIsNone(serve.mapping_problem(s, self.cfg))

    def test_every_change_to_the_key_port_is_refused(self):
        port = str(self.doc["keyport"])
        changes = {
            "no TCP": lambda s: s.pop("TCP"),
            "no Web": lambda s: s.pop("Web"),
            "TCP a list": lambda s: s.update(TCP=[]),
            "Web a list": lambda s: s.update(Web=[]),
            "AllowFunnel a list": lambda s: s.update(AllowFunnel=[]),
            "port not HTTPS": lambda s: s["TCP"].update({port: {"HTTPS": False}}),
            "port forwarded": lambda s: s["TCP"].update({port: {"TCPForward": "127.0.0.1:22"}}),
            "port HTTPS and more": lambda s: s["TCP"][port].update(TCPForward="127.0.0.1:22"),
            "port gone": lambda s: s["TCP"].pop(port),
            "site gone": lambda s: s["Web"].pop(self.hostport),
            "second host on the port": lambda s: s["Web"].update({"other.example:" + port: s["Web"][self.hostport]}),
            "site not an object": lambda s: s["Web"].update({self.hostport: []}),
            "page from elsewhere": lambda s: s["Web"][self.hostport]["Handlers"]["/"].update(Path="/srv/other/page"),
            "page proxied": lambda s: s["Web"][self.hostport]["Handlers"].update({"/": {"Proxy": "http://127.0.0.1:9"}}),
            "api elsewhere": lambda s: s["Web"][self.hostport]["Handlers"]["/api"].update(Proxy="http://127.0.0.1:9"),
            "extra handler": lambda s: s["Web"][self.hostport]["Handlers"].update({"/x": {"Text": "hi"}}),
            "no handlers": lambda s: s["Web"][self.hostport].pop("Handlers"),
            "funnel on": lambda s: s.update(AllowFunnel={self.hostport: True}),
            "funnel odd value": lambda s: s.update(AllowFunnel={self.hostport: "yes"}),
            "funnel other host same port": lambda s: s.update(AllowFunnel={"other.example:" + port: True}),
        }
        for what, change in changes.items():
            with self.subTest(what):
                s = self.status()
                change(s)
                self.assertEqual(serve.mapping_problem(s, self.cfg), "serve-changed")

    def test_foreground_services_and_unknown_keys(self):
        """Review N4: a foreground session (`tailscale serve` without --bg) or a service could put the key port on
        Funnel or map it elsewhere, and a key root does not know could mean anything: each is refused. Empty ones
        pass."""
        port = str(self.doc["keyport"])
        refused = {
            "foreground funnel": {"Foreground": {"sess1": {"TCP": {port: {"HTTPS": True}},
                                                           "AllowFunnel": {self.hostport: True}}}},
            "foreground anything": {"Foreground": {"sess1": {}}},
            "foreground a list": {"Foreground": [1]},
            "a service": {"Services": {"svc:x": {}}},
            "services zero": {"Services": 0},
            "an unknown key": {"Unknown": 1},
            "an unknown empty key": {"Later": {}},
        }
        for what, over in refused.items():
            with self.subTest(what):
                s = self.status()
                s.update(over)
                self.assertEqual(serve.mapping_problem(s, self.cfg), "serve-changed")
        s = self.status()
        s.update(Foreground={}, Services=None, AllowFunnel={})
        self.assertIsNone(serve.mapping_problem(s, self.cfg))


class ProblemTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = root_support.make_ctx(tmp.name)
        self.doc = _doc()
        self.cfg = _cfg(self.doc)
        serve_ok(self.ctx, self.doc)

    def answer(self, argv, code=0, out=""):
        self.ctx.cmd.answers[argv] = CmdResult(code, out, "")

    def test_all_in_order_passes_with_fixed_argv(self):
        self.assertIsNone(serve.problem(self.ctx, self.cfg))
        self.assertEqual(self.ctx.cmd.calls, [list(SERVE_STATUS), list(SERVE_PREFS), list(SERVE_PID)])

    def test_no_operator_key_is_no_operator(self):
        self.answer(SERVE_PREFS, out='{"WantRunning":true}')
        self.assertIsNone(serve.problem(self.ctx, self.cfg))

    def test_operator_set(self):
        serve_ok(self.ctx, self.doc, operator="someone")
        self.assertEqual(serve.problem(self.ctx, self.cfg), "operator-set")

    def test_cert_uid_set(self):
        """Review S2: read from tailscaled's environment as it runs, so it shows however it was set (Environment=,
        or EnvironmentFile=/etc/default/tailscaled, which `systemctl show -p Environment` never lists)."""
        for environ in (b"PATH=/usr/bin\0TS_PERMIT_CERT_UID=1000\0PORT=41641\0", b"TS_PERMIT_CERT_UID=\0",
                        b"FLAGS=\0TS_PERMIT_CERT_UID=caddy"):
            with self.subTest(environ=environ):
                serve_ok(self.ctx, self.doc, environ=environ)
                self.assertEqual(serve.problem(self.ctx, self.cfg), "cert-uid-set")
        for environ in (b"", b"PATH=/usr/bin\0NOT_TS_PERMIT_CERT_UID=1\0", b"PATH=/x TS_PERMIT_CERT_UID=1\0"):
            with self.subTest(environ=environ):
                serve_ok(self.ctx, self.doc, environ=environ)
                self.assertIsNone(serve.problem(self.ctx, self.cfg))

    def test_mapping_comes_first(self):
        s = serve_status(self.doc, AllowFunnel={"%s:%d" % (self.doc["rp_id"], self.doc["keyport"]): True})
        serve_ok(self.ctx, self.doc, status=s, operator="someone", environ=b"TS_PERMIT_CERT_UID=1\0")
        self.assertEqual(serve.problem(self.ctx, self.cfg), "serve-changed")
        self.assertEqual(self.ctx.cmd.calls, [list(SERVE_STATUS)])

    def test_unreadable_fails_closed(self):
        cases = {
            "status fails": (SERVE_STATUS, 1, ""),
            "status timed out": (SERVE_STATUS, 124, ""),
            "status not JSON": (SERVE_STATUS, 0, "Serve is not set up"),
            "status a list": (SERVE_STATUS, 0, "[]"),
            "status repeats a key": (SERVE_STATUS, 0, '{"TCP":{},"TCP":{}}'),
            "prefs fail": (SERVE_PREFS, 1, ""),
            "prefs not JSON": (SERVE_PREFS, 0, "OperatorUser="),
            "main pid fails": (SERVE_PID, 1, TAILSCALED_PID),
            "no main process": (SERVE_PID, 0, "0\n"),
            "main pid empty": (SERVE_PID, 0, ""),
            "main pid not a number": (SERVE_PID, 0, "../1\n"),
            "main pid of another process": (SERVE_PID, 0, "4101\n"),
        }
        for what, (argv, code, out) in cases.items():
            with self.subTest(what):
                serve_ok(self.ctx, self.doc)
                self.answer(argv, code, out)
                self.assertEqual(serve.problem(self.ctx, self.cfg), "serve-unreadable")

    def test_tailscaled_process_unreadable_fails_closed(self):
        """A pid that is not tailscaled any more (it restarted in between), an environment that does not read or is
        over 1 MB: serve-unreadable, never a pass."""
        folder = self.ctx.proc / TAILSCALED_PID
        cases = {
            "another name": lambda: (folder / "comm").write_bytes(b"sshd\n"),
            "no name": lambda: (folder / "comm").unlink(),
            "no environment": lambda: (folder / "environ").unlink(),
            "environment too big": lambda: (folder / "environ").write_bytes(b"A=" + b"x" * serve.ENVIRON_MAX),
        }
        for what, spoil in cases.items():
            with self.subTest(what):
                serve_ok(self.ctx, self.doc)
                spoil()
                self.assertEqual(serve.problem(self.ctx, self.cfg), "serve-unreadable")
        serve_ok(self.ctx, self.doc, environ=b"A=" + b"x" * (serve.ENVIRON_MAX - 2))
        self.assertIsNone(serve.problem(self.ctx, self.cfg))

    def test_every_reason_is_class_other_with_a_buzz(self):
        """The runner files each of serve's reasons as class other with the attention kind serve-changed."""
        from zeus_root import runner
        for reason in ("serve-changed", "operator-set", "cert-uid-set", "serve-unreadable"):
            with self.subTest(reason):
                r = runner.refusal(reason, "Restart Bluetooth")
                self.assertEqual((r.reason, r.cls, r.attention), (reason, "other", "serve-changed"))
                self.assertTrue(r.sentence.endswith("Nothing ran."))


if __name__ == "__main__":
    unittest.main()
