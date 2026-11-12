"""zeus-root: the reach check (root/zeus_root/reach.py, design §16.4 step 8, private design 5.6, plan Task 8, D3, D6).

After a done run of an entry that can cut the phone off, root waits up to 600 s for the phone to check in; without a
check-in the paired undo runs by itself (at a tick, at the next runner start if a timer was lost, or at boot), and a
running game delays it by at most reach_game_max_s. Linux only (flock, scripts).
"""
import json
import os
import unittest
from unittest import mock

from tests.root_support import Phone
from tests.test_root_runner import CLOSE, RunnerCase
from zeus_root import logs, reach, runner, state
from zeus_root.ctx import CmdResult, iso

CLOSE_SCRIPT = "#!/bin/sh\necho close $ZEUS_ROOT_RUN\necho ZEUS_STAGE=change\nexit 0\n"
OPEN_SCRIPT = "#!/bin/sh\necho open $ZEUS_ROOT_RUN\necho ZEUS_STAGE=change\nexit 0\n"
SYSTEMCTL = "/usr/bin/systemctl"


class ReachCase(RunnerCase):
    def setUp(self):
        super().setUp()
        self.scripts(close_media_lan=CLOSE_SCRIPT, open_media_lan=OPEN_SCRIPT)
        self.close = Phone(self.ctx, action="close-media-lan")
        self.open = Phone(self.ctx, action="open-media-lan")

    def closed(self):
        """Close the media ports (a done run that can cut the phone off): it now waits for a check-in."""
        rec = self.close.record()
        self.put(rec)
        self.start()
        return rec, self.run_of(rec)

    def reach_file(self, run, done=False):
        return self.ctx.lib / "reach" / ("done" if done else "") / (run + ".json")

    def timer(self, verb, run):
        return self.ctx.cmd.calls.count([SYSTEMCTL, verb, "zeus-root-reach@%s.timer" % run])

    def tick(self, run, code=0):
        self.assertEqual(reach.tick(self.ctx, run), code)

    def waits(self, run):
        self.assertTrue(self.reach_file(run).exists())
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "waiting")

    def put_back_lines(self, run, reason, reach_word="undone", outcome="undone"):
        urun = "u" + run[1:]
        lines = self.lines(run) + self.lines(urun)
        self.assertEqual([(x["run"], x["outcome"], x.get("reach")) for x in lines],
                         [(run, "done", reach_word), (urun, outcome, None)])
        self.assertEqual((lines[1]["of"], lines[1]["action"], lines[1]["reason"]), (run, "open-media-lan", reason))
        return lines


class WaitTest(ReachCase):
    def test_waiting_after_a_run_that_can_cut_you_off(self):
        """D6: one line per try, written when it is final: the run's line waits in its reach file (root 600)."""
        rec, run = self.closed()
        res = logs.read_result(self.ctx, run)
        self.assertEqual((res["state"], res["deadline"], res["remind_at"]),
                         ("waiting", iso(self.ctx, self.t0 + 600), iso(self.ctx, self.t0 + 300)))
        self.assertEqual(self.lines(run), [])
        self.assertEqual(self.timer("start", run), 1)
        path = self.reach_file(run)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        doc = json.loads(path.read_bytes())
        self.assertEqual((doc["action"], doc["undo"], doc["node_id"], doc["login"], doc["deadline"],
                          doc["game_wait_start"]),
                         ("close-media-lan", "open-media-lan", "nTEST1CNTRL", "owner@example.com", self.t0 + 600, None))
        self.assertEqual(doc["pending"]["outcome"], "done")
        logs.assert_no_ids(doc)
        self.assertEqual(self.attention(), [])

    def test_reminder_fields(self):
        _, run = self.closed()
        doc = json.loads(self.reach_file(run).read_bytes())
        self.assertEqual(doc["remind_at"], doc["deadline"] - 300)

    def test_kept(self):
        rec, run = self.closed()
        self.at(120)
        self.put(self.close.check_in(run))
        self.start()
        line = self.line(rec)
        self.assertEqual((line["outcome"], line["reach"], line["exit"]), ("done", "kept", 0))
        res = logs.read_result(self.ctx, run)
        self.assertEqual((res["state"], res["deadline"], res["remind_at"]), ("done", None, None))
        self.assertEqual(self.timer("stop", run), 1)
        self.assertFalse(self.reach_file(run).exists())
        self.assertTrue(self.reach_file(run, done=True).exists())
        self.at(700)
        self.tick(run)
        self.assertEqual(self.ran(), [run])
        self.assertEqual(self.attention(), [])
        self.chain_ok()

    def test_check_in_from_other_device_or_login_ignored(self):
        _, run = self.closed()
        for i, seen in enumerate(({"node_id": "nOTHERDEVICE"}, {"login": "other@example.com"})):
            with self.subTest(seen=seen):
                self.at(10 + i)
                self.put(self.close.check_in(run, **seen))
                self.start()
                line = self.lines(run)[-1]
                self.assertEqual((line["outcome"], line["reason"], line["class"]),
                                 ("refused", "check-in-refused", "garbage"))
                self.waits(run)
        self.put(self.close.check_in("r" + "9" * 20))
        self.start()
        self.assertEqual(self.lines("r" + "9" * 20)[0]["reason"], "check-in-refused")
        self.assertEqual(len([x for x in self.lines() if x.get("reason") == "check-in-refused"]), 3)
        self.assertEqual(state.refusals_in_hour(self.ctx, "counted"), 0)
        self.assertEqual(self.attention(), [])

    def test_a_check_in_after_the_deadline_comes_too_late(self):
        """The runner puts back every passed deadline before it reads the inbox, so a late check-in finds no wait."""
        _, run = self.closed()
        self.at(601)
        self.put(self.close.check_in(run))
        self.start()
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")
        self.assertEqual(self.lines(run)[-1]["reason"], "check-in-refused")

    def test_a_late_check_in_keeps_nothing_while_root_files_are_broken(self):
        """Review N1: with root's files broken no put-back runs, so the wait is still there after its deadline; a
        check-in then keeps nothing, and the put-back follows once the files are fixed."""
        _, run = self.closed()
        self.at(700)
        os.chmod(self.ctx.etc / "config.json", 0o666)
        self.put(self.close.check_in(run))
        self.start()
        self.assertEqual(self.lines(run)[-1]["reason"], "check-in-refused")
        self.waits(run)
        os.chmod(self.ctx.etc / "config.json", 0o644)
        self.at(760)
        self.start()
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")

    def test_a_late_check_in_keeps_nothing_during_the_game_wait(self):
        _, run = self.closed()
        self.game()
        self.at(700)
        self.tick(run)
        self.put(self.close.check_in(run))
        self.start()
        self.assertEqual(self.lines(run)[-1]["reason"], "check-in-refused")
        self.waits(run)
        self.at(700 + 900)
        self.tick(run)
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")


class Killed(BaseException):
    """A kill (SIGTERM, a power cut) in the middle of a finish: nothing catches it."""


class FinishTest(ReachCase):
    """Review S4: a kept or superseded wait says `final` in its reach file before its one line is written, so a kill
    at any point of the finish neither loses that line, nor writes it twice, nor lets a put-back follow."""

    def kill_once(self, target, name, when=lambda *a, **k: True):
        real, done = getattr(target, name), []

        def wrapper(*args, **kwargs):
            if not done and when(*args, **kwargs):
                done.append(1)
                raise Killed(name)
            return real(*args, **kwargs)
        return mock.patch.object(target, name, wrapper)

    def recover(self, how, run):
        if how == "start":
            self.start()
        elif how == "tick":
            self.tick(run)
        else:
            self.assertEqual(reach.boot(self.ctx), 0)

    def check_kept(self, rec, run):
        self.assertEqual([(x["outcome"], x.get("reach")) for x in self.lines(run)], [("done", "kept")])
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "done")
        self.assertFalse(self.reach_file(run).exists())
        self.assertTrue(self.reach_file(run, done=True).exists())
        self.assertEqual(self.ran(), [run])                  # never put back

    def test_killed_at_each_step_of_a_check_in(self):
        steps = {
            "before the line": (logs, "append_run", lambda ctx, line: "reach" in line),
            "before the result": (logs, "write_result", lambda ctx, run, **f: f.get("state") == "done"),
            "before the move": (reach, "_close", lambda *a: True),
            "while the timer stops": ("ctx", "cmd", lambda argv, timeout: argv[1:2] == ["stop"]),
        }
        for i, (what, (target, name, when)) in enumerate(steps.items()):
            for how in ("start", "tick", "boot"):
                with self.subTest(what=what, how=how):
                    self.setUp()
                    rec, run = self.closed()
                    self.at(120)
                    self.put(self.close.check_in(run))
                    with self.kill_once(self.ctx if target == "ctx" else target, name, when):   # the new setUp's ctx
                        with self.assertRaises(Killed):
                            runner.start(self.ctx)
                    self.at(700)                          # past the deadline: a put-back would follow if it could
                    self.recover(how, run)
                    if how != "start":
                        self.start()                      # the check-in left in work/ is a leftover now
                    self.check_kept(rec, run)
                    self.chain_ok()

    def test_killed_while_superseding(self):
        _, run = self.closed()
        self.at(10)
        opened = self.open.record()
        self.put(opened)
        with self.kill_once(reach, "_close"):
            with self.assertRaises(Killed):
                runner.start(self.ctx)
        self.assertEqual(json.loads(self.reach_file(run).read_bytes())["final"], "superseded")
        self.at(700)
        self.tick(run)
        self.assertEqual([(x["outcome"], x.get("reach")) for x in self.lines(run)], [("done", "superseded")])
        res = logs.read_result(self.ctx, run)
        self.assertEqual((res["state"], res["sentence"]),
                         ("superseded", CLOSE + " was followed by a newer run, so it is not put back."))
        self.assertNotIn("u" + run[1:], self.ran())

    def test_a_check_in_for_a_superseded_finish_is_refused(self):
        _, run = self.closed()
        doc = json.loads(self.reach_file(run).read_bytes())
        doc["final"] = "superseded"
        state.write_file(self.reach_file(run), json.dumps(doc).encode(), 0o600)
        self.at(30)
        self.put(self.close.check_in(run))
        self.start()
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "superseded")
        self.assertEqual([x.get("reach") or x.get("reason") for x in self.lines(run)],
                         ["superseded", "check-in-refused"])

    def test_an_unknown_final_is_broken(self):
        _, run = self.closed()
        doc = json.loads(self.reach_file(run).read_bytes())
        doc["final"] = "undone"
        state.write_file(self.reach_file(run), json.dumps(doc).encode(), 0o600)
        with self.assertRaises(state.Broken):
            reach.waiting(self.ctx)


class PutBackTest(ReachCase):
    def test_fired(self):
        _, run = self.closed()
        self.at(599)
        self.tick(run)
        self.waits(run)
        self.at(600)
        self.tick(run)
        urun = "u" + run[1:]
        self.put_back_lines(run, "no check-in")
        res = logs.read_result(self.ctx, run)
        self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"], res["deadline"]), ("undone", True, True, None))
        self.assertEqual(res["sentence"], CLOSE + " was put back: your phone did not check in.")
        self.assertEqual([(a["kind"], a["run"], a["sentence"]) for a in self.attention()],
                         [("put-back", run, CLOSE + " was put back: your phone did not check in")])
        self.assertIn("open " + urun, self.script_log(urun))
        self.assertTrue(self.reach_file(run, done=True).exists())
        self.assertEqual(self.timer("stop", run), 1)
        self.at(660)
        self.tick(run)                       # a later tick finds no wait: stops its timer, runs nothing
        self.assertEqual(self.timer("stop", run), 2)
        self.assertEqual(self.ran(), [run, urun])
        self.chain_ok()

    def test_put_back_whose_undo_fails(self):
        _, run = self.closed()
        with open(self.script_file("open-media-lan"), "a") as f:
            f.write("# changed after the list was made\n")
        self.at(600)
        self.tick(run)
        self.put_back_lines(run, "no check-in", reach_word="undo-failed", outcome="failed")
        res = logs.read_result(self.ctx, run)
        self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"]), ("failed", False, False))
        self.assertEqual(self.kinds(), ["undo-failed"])
        self.assertEqual(self.ran(), [run])

    def test_put_back_whose_undo_cannot_start(self):
        """Review N7: an undo that could not start did not run."""
        _, run = self.closed()
        os.chmod(self.script_file("open-media-lan"), 0o644)
        self.at(600)
        self.tick(run)
        self.put_back_lines(run, "no check-in", reach_word="undo-failed", outcome="failed")
        res = logs.read_result(self.ctx, run)
        self.assertEqual((res["state"], res["undo_ran"], res["undo_ok"]), ("failed", False, False))
        self.assertFalse(self.lines("u" + run[1:])[0]["undo_ran"])
        self.assertEqual(self.lines("u" + run[1:])[0]["exit"], 127)

    def test_game_ceiling(self):
        """A game running at the tick delays the put-back by at most reach_game_max_s (900 s), counted from the first
        tick that saw it."""
        _, run = self.closed()
        self.game()
        self.at(650)
        self.tick(run)
        self.assertEqual(json.loads(self.reach_file(run).read_bytes())["game_wait_start"], self.t0 + 650)
        self.at(650 + 899)
        self.tick(run)
        self.waits(run)
        self.at(650 + 900)
        self.tick(run)
        self.put_back_lines(run, "no check-in, after 15 minutes of game")
        self.assertEqual(logs.read_result(self.ctx, run)["sentence"],
                         CLOSE + " was put back: your phone did not check in (after 15 minutes of game).")

    def test_put_back_busy_waits_for_next_tick(self):
        _, run = self.closed()
        self.at(600)
        with self.lock_held():
            self.tick(run)
        self.waits(run)
        self.assertEqual(self.ran(), [run])
        self.at(660)
        self.tick(run)
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")

    def test_broken_root_files_hold_the_put_back(self):
        """Design §16.3: root's own files broken means nothing runs, a put-back included; the next tick tries again."""
        _, run = self.closed()
        os.chmod(self.ctx.etc / "config.json", 0o666)
        self.at(600)
        self.tick(run, code=1)
        self.waits(run)
        self.assertEqual(self.kinds(), ["file-rules"])
        os.chmod(self.ctx.etc / "config.json", 0o644)
        self.at(660)
        self.tick(run)
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")

    def test_lost_timer_handled_at_runner_start(self):
        """No tick came (the timer was lost): the next runner start puts back first, and that put-back is its one
        script, so a record in the same start is refused busy."""
        _, run = self.closed()
        self.at(601)
        other = Phone(self.ctx).record()
        self.put(other)
        self.start()
        self.put_back_lines(run, "no check-in")
        self.assertEqual(self.line(other)["reason"], "busy")


class SupersedeTest(ReachCase):
    def test_pair_superseded(self):
        """S10: a done run of the pair's other side ends the waiting check as superseded; it is never put back."""
        _, run = self.closed()
        self.at(10)
        opened = self.open.record()
        self.put(opened)
        self.start()
        lines = self.lines(run)
        self.assertEqual([(x["outcome"], x["reach"]) for x in lines], [("done", "superseded")])
        res = logs.read_result(self.ctx, run)
        self.assertEqual((res["state"], res["sentence"]),
                         ("superseded", CLOSE + " was followed by a newer run, so it is not put back."))
        self.assertEqual(self.timer("stop", run), 1)
        self.waits(self.run_of(opened))
        self.at(700)
        self.tick(run)
        self.assertEqual(sorted(self.ran()), sorted([run, self.run_of(opened)]))
        self.assertEqual(self.attention(), [])

    def test_same_action_again_supersedes_the_older_wait(self):
        _, first = self.closed()
        self.at(200)
        _, second = self.closed()
        self.assertEqual(logs.read_result(self.ctx, first)["state"], "superseded")
        self.waits(second)
        self.assertEqual(len(reach.waiting(self.ctx)), 1)

    def test_superseded_only_on_done(self):
        _, run = self.closed()
        self.scripts(open_media_lan="#!/bin/sh\necho checking\nexit 1\n")
        self.at(10)
        opened = self.open.record()
        self.put(opened)
        self.start()
        self.assertEqual(logs.read_result(self.ctx, self.run_of(opened))["state"], "stopped")
        self.waits(run)


class BootTest(ReachCase):
    def test_restart_in_the_middle_deadline_passed(self):
        """At boot a passed deadline is put back at once, with no game wait: no check-in is trusted across a restart."""
        _, run = self.closed()
        self.game()
        self.at(700)
        self.assertEqual(reach.boot(self.ctx), 0)
        self.put_back_lines(run, "no check-in, after a restart")
        self.assertEqual(logs.read_result(self.ctx, run)["sentence"],
                         CLOSE + " was put back: your phone did not check in (after a restart).")

    def test_restart_in_the_middle_deadline_not_passed(self):
        _, run = self.closed()
        self.at(100)
        self.assertEqual(reach.boot(self.ctx), 0)
        self.assertEqual(self.timer("start", run), 2)
        self.waits(run)
        self.assertEqual(self.ran(), [run])

    def test_boot_timer_that_will_not_start_buzzes(self):
        _, run = self.closed()
        self.ctx.cmd.answers[(SYSTEMCTL, "start", "zeus-root-reach@%s.timer" % run)] = CmdResult(1, "", "failed")
        self.at(100)
        self.assertEqual(reach.boot(self.ctx), 0)
        self.assertEqual(self.kinds(), ["watcher-failed"])
        self.waits(run)

    def test_boot_quarantines_leftovers(self):
        rec = Phone(self.ctx).record()
        self.put(rec, folder="work")
        self.assertEqual(reach.boot(self.ctx), 0)
        self.assertEqual(self.folder("work"), [])
        self.assertIsNotNone(self.kept_until(rec["nonce"]))

    def test_boot_with_broken_files_runs_nothing(self):
        _, run = self.closed()
        os.chmod(self.ctx.etc / "config.json", 0o666)
        self.at(700)
        self.assertEqual(reach.boot(self.ctx), 1)
        self.waits(run)
        self.assertEqual(self.kinds(), ["file-rules"])

    def test_boot_with_broken_files_restarts_the_timers(self):
        """Review S3: the timers start first, so once the owner fixes the files a tick puts back; nothing needs the
        phone the change may have cut off."""
        _, run = self.closed()
        os.chmod(self.ctx.etc / "config.json", 0o666)
        self.at(100)
        self.assertEqual(reach.boot(self.ctx), 1)
        self.assertEqual(self.timer("start", run), 2)
        os.chmod(self.ctx.etc / "config.json", 0o644)
        self.at(700)
        self.tick(run)
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")

    def test_boot_with_the_lock_held_restarts_the_timers(self):
        rec = Phone(self.ctx).record()
        _, run = self.closed()
        self.put(rec, folder="work")
        self.at(100)
        with self.lock_held():
            self.assertEqual(reach.boot(self.ctx), 1)
        self.assertEqual(self.timer("start", run), 2)
        self.waits(run)
        self.assertEqual(len(self.folder("work")), 1)        # leftovers wait for the lock
        self.at(700)
        self.tick(run)
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")

    def test_a_timer_that_does_not_start_at_begin(self):
        """The wait still ends: at the next runner start or at boot (due), and the owner hears the timer failed."""
        from zeus_root import canon
        nonce = "A" * 42 + "E"
        run = canon.run_id(nonce)
        self.ctx.cmd.answers[(SYSTEMCTL, "start", "zeus-root-reach@%s.timer" % run)] = CmdResult(1, "", "")
        self.put(self.close.record(nonce=nonce))
        self.start()
        self.waits(run)
        self.assertEqual([(a["kind"], a["run"]) for a in self.attention()], [("watcher-failed", run)])
        self.at(601)
        self.start()
        self.assertEqual(logs.read_result(self.ctx, run)["state"], "undone")


if __name__ == "__main__":
    unittest.main()
