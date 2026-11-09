"""The decision inbox (docs/design.md §14, tier 3; zeus/asks.py): an ask is open until it is answered once or expires;
every change is a signed record and a signed log line that `zeus verify-log` checks; a second answer, an answer to an
expired ask and an answer to a record changed outside Zeus are refused; nothing here starts a process."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from zeus import asks, init, log, queue, verify

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 9, 9, 0, 0, tzinfo=timezone(timedelta(hours=-7)))
OPTS = ["Yes, as proposed", "Tier 1 without the second tap"]


class AsksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.data = self.home / ".local" / "share" / "zeus"
        self.secret = self.home / ".config" / "zeus" / "secret.env"
        self.env = {k: v for k, v in os.environ.items() if k != "SSH_CONNECTION"}
        self.env.update(HOME=str(self.home), USERPROFILE=str(self.home), ZEUS_DATA=str(self.data),
                        ZEUS_SECRET=str(self.secret), LEAFY_REALM=str(self.home / "none.json"))
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        init.run(lambda s: None)
        self.store = queue.Store()

    def ask(self, **kw):
        args = dict(project="zeus", title="Tiers as proposed?", options=list(OPTS), context="Tier 1 = rules.",
                    expires="3d", now=NOW)
        args.update(kw)
        return asks.ask(self.store, **args)

    def lines(self):
        return [e for _, _, e in log.entries(self.store)]

    def refused(self, code, text, fn, *a, **kw):
        with self.assertRaises(asks.AskError) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code, str(cm.exception))
        self.assertIn(text, str(cm.exception))

    # ---- open ----
    def test_ask_writes_an_open_signed_record_and_a_signed_line(self):
        rec = self.ask()
        self.assertEqual(rec["id"], "ask-20261109-090000-zeus")
        self.assertTrue((self.data / "queue" / "ask-20261109-090000-zeus.json").is_file())
        self.assertEqual((rec["kind"], rec["tier"], rec["state"], rec["project"], rec["options"], rec["answer"]),
                         ("decision", 3, "open", "zeus", OPTS, None))
        self.assertEqual((rec["asked"], rec["expires"]), ("2026-11-09T09:00:00-07:00", "2026-11-12T09:00:00-07:00"))
        self.assertEqual(rec["by"]["via"], "local")
        self.assertTrue(log.check(rec))
        self.assertEqual(asks.read(self.store, rec["id"]), rec)
        (line,) = self.lines()
        self.assertEqual((line["action"], line["id"], line["tier"], line["kind"], line["options"], line["expires"]),
                         ("ask", rec["id"], 3, "decision", OPTS, rec["expires"]))
        self.assertTrue(log.check(line))
        self.assertEqual(self.ask()["id"], "ask-20261109-090000-zeus-2", "a second ask in the same second gets its own id")

    def test_an_ask_over_ssh_says_so(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.4 5 6 7"}):
            self.assertEqual(self.ask()["by"]["via"], "ssh")

    def test_user_flag_over_ssh_is_refused(self):
        rec = self.ask()
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.4 5 6 7"}):
            for kw in (dict(user="owner@example.com", ip="203.0.113.7"), dict(user="owner@example.com"),
                       dict(ip="203.0.113.7")):
                self.refused(2, "--user and --ip are refused in an ssh session", asks.answer, self.store, rec["id"],
                             index=1, now=NOW, **kw)
            self.assertEqual(asks.read(self.store, rec["id"])["state"], "open", "nothing was recorded")
            self.assertEqual([l["action"] for l in self.lines()], ["ask"])
            r = self.cli_env({"SSH_CONNECTION": "192.0.2.4 5 6 7"}, "answer", rec["id"], "--index", "1",
                             "--user", "owner@example.com", "--ip", "203.0.113.7")
            self.assertEqual(r.returncode, 2, r.stderr)
            self.assertIn("refused in an ssh session", r.stderr)
            got = asks.answer(self.store, rec["id"], index=1, now=NOW)
        self.assertEqual((got["answer"]["by"]["via"], got["answer"]["by"]["ip"]), ("ssh", "192.0.2.4"),
                         "over ssh an answer is the shell's, never a tap")

    def test_user_and_ip_go_together(self):
        rec = self.ask()
        for kw in (dict(user="owner@example.com"), dict(ip="203.0.113.7")):
            self.refused(2, "--user and --ip go together", asks.answer, self.store, rec["id"], index=1, now=NOW, **kw)
        self.assertEqual(asks.read(self.store, rec["id"])["state"], "open")

    def test_bad_fields_ask_nothing(self):
        for kw, text in ((dict(project="Zeus!"), "--project"), (dict(title=""), "--title"),
                         (dict(title="two\nlines"), "--title"), (dict(title="x" * 201), "--title"),
                         (dict(options=["only one"]), "2 to 8"), (dict(options=["a"] * 9), "2 to 8"),
                         (dict(options=["Yes", "yes"]), "differ"), (dict(options=["a", "b\tc"]), "--option"),
                         (dict(options=["a", "x" * 81]), "--option"), (dict(context="x" * 2001), "--context"),
                         (dict(context="bell\x07"), "--context"), (dict(expires="3 days"), "--expires"),
                         (dict(expires="5m"), "between 10m and 30d"), (dict(expires="31d"), "between 10m and 30d")):
            self.refused(2, text, self.ask, **kw)
        self.assertEqual(asks.all_(self.store), [])
        self.assertEqual(self.lines(), [])

    def test_no_key_asks_nothing(self):
        self.secret.unlink()
        self.refused(1, "run `zeus init`", self.ask)
        self.assertEqual(asks.all_(self.store), [])

    # ---- answered ----
    def test_answer_by_text_and_by_number(self):
        a, b = self.ask(), self.ask(project="hub")
        got = asks.answer(self.store, a["id"], option=OPTS[1], now=NOW + timedelta(minutes=5))
        self.assertEqual((got["state"], got["answer"]["option"], got["answer"]["index"], got["answer"]["at"]),
                         ("answered", OPTS[1], 2, "2026-11-09T09:05:00-07:00"))
        self.assertTrue(log.check(got), "the answered record is re-signed")
        got = asks.answer(self.store, b["id"], index=1, user="owner@example.com", ip="203.0.113.7", now=NOW)
        self.assertEqual(got["answer"]["by"], {"user": "owner@example.com", "via": "elaia", "ip": "203.0.113.7"})
        answers = [l for l in self.lines() if l["action"] == "answer"]
        self.assertEqual([(l["id"], l["option"], l["index"], l["tier"]) for l in answers],
                         [(a["id"], OPTS[1], 2, 3), (b["id"], OPTS[0], 1, 3)])
        self.assertTrue(all(log.check(l) for l in self.lines()))

    def test_a_second_answer_is_refused_and_changes_nothing(self):
        rec = self.ask()
        asks.answer(self.store, rec["id"], index=1, now=NOW)
        path = self.data / "queue" / (rec["id"] + ".json")
        before, n = path.read_bytes(), len(self.lines())
        for kw in (dict(index=2), dict(index=1), dict(option=OPTS[1])):
            self.refused(1, "already answered ('Yes, as proposed'); an answer can't change", asks.answer, self.store,
                         rec["id"], now=NOW + timedelta(minutes=1), **kw)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(len(self.lines()), n, "no second line")

    def test_two_answers_at_once_only_one_wins(self):
        rec = self.ask()
        (self.data / "queue" / ".answers").mkdir(parents=True)
        (self.data / "queue" / ".answers" / (rec["id"] + ".json")).write_text("{}", encoding="utf-8")
        self.refused(1, "answered a moment ago", asks.answer, self.store, rec["id"], index=2, now=NOW)
        self.assertEqual(asks.read(self.store, rec["id"])["state"], "open")
        self.assertEqual([l["action"] for l in self.lines()], ["ask"])

    def test_wrong_option_number_or_id(self):
        rec = self.ask()
        self.refused(2, "not one of the options", asks.answer, self.store, rec["id"], option="Maybe", now=NOW)
        for i in (0, 3, -1):
            self.refused(2, "--index is a number from 1 to 2", asks.answer, self.store, rec["id"], index=i, now=NOW)
        self.refused(2, "is not an ask id", asks.answer, self.store, "../../secret", index=1, now=NOW)
        self.refused(2, "is not an ask id", asks.answer, self.store, "20261021-090012-app-1234567", index=1, now=NOW)
        self.refused(1, "no ask ask-20261109-090000-nobody", asks.answer, self.store, "ask-20261109-090000-nobody",
                     index=1, now=NOW)
        self.assertEqual(asks.read(self.store, rec["id"])["state"], "open")

    # ---- expired ----
    def test_an_expired_ask_cannot_be_answered(self):
        rec = self.ask(expires="10m")
        self.refused(1, "is expired, not open", asks.answer, self.store, rec["id"], index=1, now=NOW + timedelta(minutes=10))
        got = asks.read(self.store, rec["id"])
        self.assertEqual((got["state"], got["answer"]), ("expired", None))
        self.assertTrue(log.check(got))
        self.assertEqual([l["action"] for l in self.lines()], ["ask", "ask-expired"])
        asks.listing(self.store, "all", now=NOW + timedelta(hours=1))
        self.assertEqual([l["action"] for l in self.lines()], ["ask", "ask-expired"], "expired once, logged once")

    def test_listing_open_all_project_and_one(self):
        a = self.ask(expires="1h")
        b = self.ask(project="hub", now=NOW + timedelta(seconds=1))
        c = self.ask(project="spartamelee", now=NOW + timedelta(seconds=2))
        asks.answer(self.store, c["id"], index=1, now=NOW + timedelta(minutes=1))
        later = NOW + timedelta(hours=2)
        self.assertEqual([r["id"] for r in asks.listing(self.store, "open", now=later)], [b["id"]])
        self.assertEqual([(r["id"], r["state"]) for r in asks.listing(self.store, "all", now=later)],
                         [(c["id"], "answered"), (b["id"], "open"), (a["id"], "expired")])
        self.assertEqual([r["id"] for r in asks.listing(self.store, "all", now=later, project="hub")], [b["id"]])
        self.assertEqual([r["id"] for r in asks.listing(self.store, now=later, aid=c["id"])], [c["id"]],
                         "asking for one id shows it whatever its state")

    # ---- signed records ----
    def test_a_record_changed_outside_zeus_is_not_answered_and_verify_log_names_it(self):
        rec = self.ask()
        path = self.data / "queue" / (rec["id"] + ".json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["options"][0] = "Deploy everything"
        path.write_text(json.dumps(doc), encoding="utf-8")
        self.refused(1, "was changed outside Zeus", asks.answer, self.store, rec["id"], index=1, now=NOW)
        r = subprocess.run([sys.executable, "-m", "zeus", "verify-log"], cwd=ROOT, capture_output=True, text=True,
                           env=dict(self.env))
        self.assertEqual(r.returncode, verify.BAD_EXIT, r.stdout + r.stderr)
        self.assertIn("%s.json:1: bad (sig does not match: the record was changed outside Zeus" % rec["id"], r.stdout)
        self.assertIn("approvals-2026-11.jsonl:1: ok (ask %s" % rec["id"], r.stdout)

    def test_a_changed_record_is_left_out_of_asks(self):
        a = self.ask()
        b = self.ask(project="hub", now=NOW + timedelta(seconds=1))
        asks.answer(self.store, b["id"], index=1, now=NOW)
        path = self.data / "queue" / (b["id"] + ".json")
        doc = json.loads(path.read_text(encoding="utf-8"))
        doc["answer"]["option"] = "FORGED"
        path.write_text(json.dumps(doc), encoding="utf-8")
        bad = []
        self.assertEqual([r["id"] for r in asks.listing(self.store, "all", now=NOW, bad=bad)], [a["id"]])
        self.assertEqual(bad, [b["id"]])
        self.assertEqual(asks.listing(self.store, now=NOW, aid=b["id"]), [], "not even when asked for by its id")
        r = self.cli("asks", "--all", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x["id"] for x in json.loads(r.stdout)], [a["id"]])
        self.assertNotIn("FORGED", r.stdout)
        self.assertIn("left out 1 record changed outside Zeus", r.stderr)
        self.assertIn(b["id"], r.stderr)
        self.assertIn("zeus verify-log", r.stderr)
        r = self.cli("asks", "--all")
        self.assertNotIn("FORGED", r.stdout)
        self.assertIn("left out 1 record changed outside Zeus", r.stderr)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["answer"]["option"], "FORGED",
                         "the changed record stays as it is, for verify-log")

    def test_no_key_lists_without_a_traceback(self):
        rec = self.ask(expires="10m")
        self.secret.unlink()
        self.refused(1, "run `zeus init`", asks.listing, self.store, "all", now=NOW + timedelta(hours=1))
        for args in (("asks",), ("asks", "--all", "--json")):
            r = self.cli(*args)
            self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
            self.assertNotIn("Traceback", r.stderr)
            self.assertIn("zeus asks:", r.stderr)
            self.assertIn("run `zeus init`", r.stderr)
            self.assertEqual(r.stdout, "")
        self.assertEqual(asks.read(self.store, rec["id"])["state"], "open", "nothing changed without the key")

    def test_verify_log_checks_the_lifecycle(self):
        a, b = self.ask(), self.ask(project="hub", expires="10m")
        asks.answer(self.store, a["id"], index=2, now=NOW)
        asks.expire(self.store, NOW + timedelta(minutes=11))
        r = subprocess.run([sys.executable, "-m", "zeus", "verify-log"], cwd=ROOT, capture_output=True, text=True,
                           env=dict(self.env))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for want in ("approvals-2026-11.jsonl:1: ok (ask %s" % a["id"], "approvals-2026-11.jsonl:3: ok (answer %s" % a["id"],
                     "approvals-2026-11.jsonl:4: ok (ask-expired %s" % b["id"],
                     "%s.json:1: ok (ask %s answered)" % (a["id"], a["id"]), "%s.json:1: ok (ask %s expired)" % (b["id"], b["id"])):
            self.assertIn(want, r.stdout)
        self.assertTrue(r.stdout.rstrip().endswith("zeus verify-log: 6 lines in 3 files, 6 ok, 0 bad"), r.stdout)
        rows = verify.verify(verify.files(self.data / "queue"), bytes.fromhex(init.read_key()))
        self.assertEqual([v for _, _, v, _ in rows], ["ok", "ok"], "a folder of ask records can be checked on its own")

    # ---- the deploy queue is not disturbed ----
    def test_asks_never_show_as_deploys_and_never_start_a_process(self):
        with mock.patch.object(subprocess, "run", lambda *a, **kw: self.fail("a subprocess was started")), \
             mock.patch.object(queue, "git", lambda *a, **kw: self.fail("git was called")):
            rec = self.ask()
            asks.answer(self.store, rec["id"], index=1, now=NOW)
            asks.listing(self.store, "all", now=NOW)
        self.assertEqual(self.store.pending(), [])
        self.assertEqual(self.store.pending(queue.WAITING), [])
        self.assertEqual(self.store.history(), [])
        self.assertEqual(queue.expire(self.store, NOW + timedelta(days=9)), [])

    # ---- the command line ----
    def cli(self, *args):
        return self.cli_env({}, *args)

    def cli_env(self, extra, *args):
        return subprocess.run([sys.executable, "-m", "zeus", *args], cwd=ROOT, capture_output=True, text=True,
                              env=dict(self.env, **extra))

    def test_the_command_line_round_trip(self):
        r = self.cli("ask", "--project", "zeus", "--title", 'Ship <b>"it"</b>?', "--option", "yes", "--option", "no",
                     "--context", "line one\nline two", "--expires", "2d", "--json")
        self.assertEqual(r.returncode, 0, r.stderr)
        rec = json.loads(r.stdout)
        self.assertEqual((rec["title"], rec["context"], rec["state"]), ('Ship <b>"it"</b>?', "line one\nline two", "open"))
        r = self.cli("asks", "--json")
        self.assertEqual([x["id"] for x in json.loads(r.stdout)], [rec["id"]])
        r = self.cli("answer", rec["id"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("give the option's text or --index N", r.stderr)
        r = self.cli("answer", rec["id"], "no")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('answered (zeus) "Ship <b>\\"it\\"</b>?" -> "no" by', r.stdout)
        r = self.cli("answer", rec["id"], "--index", "1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("already answered ('no')", r.stderr)
        self.assertEqual(self.cli("asks").stdout.strip(), "zeus asks: nothing is waiting for an answer")
        got = json.loads(self.cli("asks", "--all", "--json").stdout)
        self.assertEqual((got[0]["state"], got[0]["answer"]["option"]), ("answered", "no"))
        self.assertEqual(json.loads(self.cli("asks", rec["id"], "--json").stdout)[0]["id"], rec["id"])
        r = self.cli("ask", "--project", "zeus", "--title", "x", "--option", "only")
        self.assertEqual(r.returncode, 2)
        self.assertIn("Nothing was asked", r.stderr)


if __name__ == "__main__":
    unittest.main()
