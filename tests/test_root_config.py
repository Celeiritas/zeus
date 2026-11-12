"""zeus-root: config.json, the fixed list (contract §8a), keys, owners, labels, and the file rules (design §16.3)."""
import hashlib
import json
import os
import tempfile
import unittest

from tests import root_support
from zeus_root import canon, config
from zeus_root.config import ListInvalid


def entry(doc, action_id):
    return next(e for e in doc["actions"] if e["id"] == action_id)


class ConfigCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ctx = root_support.make_ctx(tmp.name)

    def refused(self, change, why=None):
        root_support.install_list(self.ctx, change)
        with self.assertRaises(ListInvalid) as cm:
            config.load_actions(self.ctx)
        if why:
            self.assertIn(why, str(cm.exception))


class ListTest(ConfigCase):
    def test_fixture_list_loads(self):
        root_support.install_list(self.ctx)
        actions, _ = config.load_actions(self.ctx)
        self.assertEqual(sorted(actions), ["close-media-lan", "open-media-lan", "restart-bluetooth",
                                           "restart-resolved", "self-test"])
        close = actions["close-media-lan"]
        self.assertEqual((close.kind, close.undo, close.can_cut_you_off, close.max_per_day, close.min_gap_s),
                         ("change", "open-media-lan", True, 6, 120))
        self.assertIsNone(actions["restart-bluetooth"].undo)

    def test_list_sha_is_file_bytes(self):
        raw = root_support.install_list(self.ctx)
        _, sha = config.load_actions(self.ctx)
        self.assertEqual(sha, hashlib.sha256(raw).hexdigest())
        root_support.install_list(self.ctx, lambda d: d["actions"].reverse())  # same entries, other bytes
        self.assertNotEqual(config.load_actions(self.ctx)[1], sha)

    def test_list_refuses_bad_id(self):
        for bad in ("Close", "close_media", "", "check-key"):
            with self.subTest(id=bad):
                self.refused(lambda d: entry(d, "restart-resolved").update(id=bad))

    def test_list_refuses_repeated_id(self):
        self.refused(lambda d: entry(d, "restart-resolved").update(id="restart-bluetooth"), "twice")

    def test_list_refuses_bad_kind(self):
        self.refused(lambda d: entry(d, "restart-resolved").update(kind="restart"))

    def test_list_refuses_bad_script(self):
        for bad in ("../x.sh", "a/b.sh", "x.py", "x.sh\n", ".sh", "x sh.sh"):
            with self.subTest(script=bad):
                self.refused(lambda d: entry(d, "restart-resolved").update(script=bad))

    def test_list_refuses_bad_sha256(self):
        for bad in ("A" * 64, "0" * 63, "0" * 65, "g" * 64):
            with self.subTest(sha=bad):
                self.refused(lambda d: entry(d, "restart-resolved").update(sha256=bad))

    def test_list_refuses_missing_undo(self):
        self.refused(lambda d: entry(d, "close-media-lan").update(undo="nope"), "pair")

    def test_list_refuses_one_way_pair(self):
        def change(d):
            entry(d, "open-media-lan").update(undo=None, can_cut_you_off=False)
        self.refused(change, "pair")

    def test_list_refuses_self_undo(self):
        def change(d):
            entry(d, "restart-bluetooth").update(undo="restart-bluetooth")
        self.refused(change, "pair")

    def test_undo_null_needs_cannot_cut_off(self):
        """v3: a restart has nothing to put back, so it may not start a reach check."""
        self.refused(lambda d: entry(d, "restart-bluetooth").update(can_cut_you_off=True), "can_cut_you_off")

    def test_list_refuses_check_with_undo_or_cut_off(self):
        def paired(d):
            entry(d, "self-test").update(undo="restart-bluetooth")
            entry(d, "restart-bluetooth").update(undo="self-test")
        self.refused(paired, "check")

    def test_list_refuses_limits(self):
        for field, bad in (("max_per_day", 0), ("max_per_day", 101), ("min_gap_s", -1), ("max_per_day", True)):
            with self.subTest(field=field, value=bad):
                self.refused(lambda d: entry(d, "restart-resolved").update({field: bad}))

    def test_list_refuses_odd_shapes(self):
        changes = [lambda d: entry(d, "self-test").update(extra=1),
                   lambda d: entry(d, "self-test").pop("added"),
                   lambda d: entry(d, "self-test").update(changes=[["a", "b"]]),
                   lambda d: entry(d, "self-test").update(changes=[["a", "b", 3]]),
                   lambda d: entry(d, "self-test").update(title=""),
                   lambda d: d.update(more=1)]
        for i, change in enumerate(changes):
            with self.subTest(i=i):
                self.refused(change)
        (self.ctx.etc / "actions.json").write_bytes(b"[]")
        with self.assertRaises(ListInvalid):
            config.load_actions(self.ctx)
        os.remove(self.ctx.etc / "actions.json")
        with self.assertRaises(ListInvalid):
            config.load_actions(self.ctx)


class ConfigFileTest(ConfigCase):
    def test_config_loads(self):
        root_support.install_config(self.ctx)
        cfg = config.load_config(self.ctx)
        self.assertEqual((cfg.rp_id, cfg.origin, cfg.keyport, cfg.gateport, cfg.reach_game_max_s),
                         ("h.example", "https://h.example:4443", 4443, 841, 900))
        root_support.install_config(self.ctx, lambda d: d.pop("reach_game_max_s"))
        self.assertEqual(config.load_config(self.ctx).reach_game_max_s, 900)

    def test_config_refuses(self):
        bad = [dict(origin="https://h.example:4444"), dict(origin="https://other.example:4443"),
               dict(keyport=443, origin="https://h.example:443"), dict(gateport=2000), dict(gateport=4443),
               dict(rp_id="H.example"), dict(page_dir="relative/page"), dict(page_dir="/usr/../tmp"),
               dict(games=["a/b"]), dict(games="steam"), dict(reach_game_max_s=-1), dict(reach_game_max_s=7200),
               dict(extra=1)]
        for change in bad:
            with self.subTest(change=change):
                root_support.install_config(self.ctx, lambda d: d.update(change))
                with self.assertRaises(config.ConfigInvalid):
                    config.load_config(self.ctx)


class KeysTest(ConfigCase):
    def test_keys_roundtrip_and_labels_have_no_ids(self):
        root_support.install_list(self.ctx)
        actions, _ = config.load_actions(self.ctx)
        a = root_support.make_key()
        b = root_support.make_key("open-media-lan", alg=-257, be=False, revoked="2026-11-13T09:00:00+00:00")
        self.assertEqual(config.load_keys(self.ctx), {})
        config.save_keys(self.ctx, {a.ref: a, b.ref: b})
        self.assertEqual(config.load_keys(self.ctx), {a.ref: a, b.ref: b})
        config.save_labels(self.ctx, config.load_keys(self.ctx), actions)
        raw = (self.ctx.etc / "labels.json").read_bytes()
        for secret in (a.id, a.user, a.spki, b.id, b.user):
            self.assertNotIn(canon.b64u_encode(secret).encode(), raw)
        labels = json.loads(raw)["labels"]
        self.assertEqual({tuple(sorted(row)) for row in labels},
                         {("action", "device", "kind", "label", "made", "ref", "revoked", "title")})
        row = next(r for r in labels if r["ref"] == a.ref)
        self.assertEqual((row["title"], row["kind"], row["device"]),
                         ("Close the media ports to the apartment network", "change", "phone"))

    def test_keys_refuse_odd_entries(self):
        a = root_support.make_key()
        config.save_keys(self.ctx, {a.ref: a})
        good = json.loads((self.ctx.etc / "keys.json").read_bytes())
        bad = [dict(ref="0" * 16), dict(alg=-8), dict(credentialId="AB"), dict(userId=""), dict(action="Close"),
               dict(be=1), dict(extra=1)]
        for change in bad:
            with self.subTest(change=change):
                doc = json.loads(json.dumps(good))
                doc["keys"][0].update(change)
                (self.ctx.etc / "keys.json").write_bytes(json.dumps(doc).encode())
                with self.assertRaises(config.KeysInvalid):
                    config.load_keys(self.ctx)
        doc = json.loads(json.dumps(good))
        doc["keys"].append(doc["keys"][0])
        (self.ctx.etc / "keys.json").write_bytes(json.dumps(doc).encode())
        with self.assertRaises(config.KeysInvalid):
            config.load_keys(self.ctx)

    def test_owners_roundtrip(self):
        self.assertEqual(config.load_owners(self.ctx), [])
        config.save_owners(self.ctx, ["b@example.com", "a@example.com", "a@example.com"])
        self.assertEqual(config.load_owners(self.ctx), ["a@example.com", "b@example.com"])
        with self.assertRaises(ValueError):
            config.save_owners(self.ctx, ["has space@example.com"])


class FileRulesTest(ConfigCase):
    def setUp(self):
        super().setUp()
        root_support.need("linux")
        root_support.install_list(self.ctx)
        root_support.install_config(self.ctx)
        root_support.good_tree(self.ctx)

    def test_correct_tree(self):
        self.assertEqual(config.file_problems(self.ctx), [])
        for name in ("keys.json", "owners.json"):
            self.assertEqual(os.stat(self.ctx.etc / name).st_mode & 0o777, 0o640)

    def test_world_writable_script(self):
        script = self.ctx.libexec / "actions" / "close-media-lan-v2.sh"
        os.chmod(script, 0o757)
        self.assertOne(script)

    def test_symlinked_list(self):
        real = self.ctx.etc / "actions.real.json"
        os.rename(self.ctx.etc / "actions.json", real)
        os.symlink(real, self.ctx.etc / "actions.json")
        self.assertOne(self.ctx.etc / "actions.json")

    def test_keys_readable_by_all(self):
        os.chmod(self.ctx.etc / "keys.json", 0o644)
        self.assertOne(self.ctx.etc / "keys.json")

    def test_inbox_open_to_others(self):
        os.chmod(self.ctx.run / "inbox", 0o733)
        self.assertOne(self.ctx.run / "inbox")

    def test_folder_inside_actions(self):
        (self.ctx.libexec / "actions" / "more").mkdir(mode=0o755)
        self.assertOne(self.ctx.libexec / "actions" / "more")

    def test_missing_owners(self):
        os.remove(self.ctx.etc / "owners.json")
        self.assertOne(self.ctx.etc / "owners.json")

    def assertOne(self, path):
        problems = config.file_problems(self.ctx)
        self.assertEqual(len(problems), 1, problems)
        self.assertTrue(problems[0].startswith(str(path) + ":"), problems)


if __name__ == "__main__":
    unittest.main()
