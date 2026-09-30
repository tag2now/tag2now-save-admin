"""tdt_admin.py commands, run against a temp database and save directory."""
import os
import json
import unittest

import tdt_admin as ta
from tests.support import RpcnDataCase, char_state, make_save, read


def sealed(buf):
    return ta.checksum(buf) == ta.be32(buf, 0)


class ShowTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.path = self.add_account("Alice", make_save(chars=[(0, 30, 1234, 10, 5)], account_rank=30))

    def test_json(self):
        code, out, _ = self.run_cli("show", "Alice", "--json")
        d = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((d["npid"], d["user_id"], d["data_id"], d["file"]), ("Alice", 1, 1001, self.path))
        self.assertEqual((d["checksum_ok"], d["account_rank"]), (True, 30))
        self.assertEqual([(c["character"], c["rank_name"], c["points"]) for c in d["chars"]],
                         [("Paul", "Byakko", 1234)])

    def test_text(self):
        code, out, _ = self.run_cli("show", "Alice")
        self.assertEqual(code, 0)
        self.assertIn("Alice  user_id=1  data_id=1001", out)
        self.assertIn("  OK\n", out)
        self.assertIn("rank=30 Byakko (빨강단)", out)

    def test_reports_a_broken_checksum(self):
        with open(self.path, "r+b") as f:
            f.write(b"\0\0\0\0")
        _, out, _ = self.run_cli("show", "Alice", "--json")
        self.assertFalse(json.loads(out)["checksum_ok"])

    def test_input_file_needs_no_account(self):
        code, out, _ = self.run_cli("show", "--input-file", self.path, "--json")
        self.assertEqual((code, json.loads(out)["account_rank"]), (0, 30))


class AccountNameTest(RpcnDataCase):
    def test_matches_case_insensitively(self):
        self.add_account("Alice", make_save())
        code, out, _ = self.run_cli("show", "aLiCe", "--json")
        self.assertEqual((code, json.loads(out)["npid"]), (0, "Alice"))

    def test_unknown_account(self):
        code, _, err = self.run_cli("show", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("account 'nobody' not found", err)

    def test_name_matching_two_accounts(self):
        self.add_account("abc", make_save())
        self.add_account("ABC", make_save())
        code, _, err = self.run_cli("show", "abc")
        self.assertEqual(code, 1)
        self.assertIn("matches 2 accounts: abc, ABC", err)

    def test_account_without_a_save(self):
        self.add_account("Alice")
        code, _, err = self.run_cli("show", "Alice")
        self.assertEqual(code, 1)
        self.assertIn("no NPWR02973_00 save for account 'Alice'", err)


class SetRankTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.original = bytes(make_save(chars=[(0, 20, 1500, 10, 5)], account_rank=20))
        self.path = self.add_account("Alice", self.original)

    def test_writes_rank_and_points(self):
        code, _, _ = self.run_cli("set-rank", "Alice", "--char", "paul", "--rank", "genbu", "--points", "3000")
        after = read(self.path)
        self.assertEqual(code, 0)
        self.assertEqual(char_state(after, 0), (29, 3000, 0))
        self.assertTrue(sealed(after))

    def test_keeps_points_when_none_given(self):
        self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29")
        self.assertEqual(char_state(read(self.path), 0), (29, 1500, 0))

    def test_backs_up_the_save_it_replaces(self):
        self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29")
        (name,) = self.backups("Alice")
        self.assertEqual(read(os.path.join(self.backup_dir, "Alice", name)), self.original)

    def test_appends_to_the_audit_log(self):
        self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29", "--points", "3000")
        (rec,) = self.audit_records()
        self.assertEqual((rec["action"], rec["npid"], rec["data_id"]), ("set-rank", "Alice", 1001))
        self.assertEqual((rec["char"], rec["rank"], rec["points"], rec["verified"]), (0, 29, 3000, True))
        self.assertEqual(rec["checksum"], f"0x{ta.be32(read(self.path), 0):08X}")

    def test_dry_run_writes_nothing(self):
        code, out, _ = self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("rank 20 Berserker -> 29 Genbu", out)
        self.assertEqual((read(self.path), self.backups("Alice"), self.audit_records()), (self.original, [], []))

    def test_refuses_an_online_account(self):
        self.online = {"Alice"}
        code, _, err = self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29")
        self.assertEqual(code, 1)
        self.assertIn("Alice is online right now", err)
        self.assertEqual(read(self.path), self.original)

    def test_force_writes_to_an_online_account(self):
        self.online = {"Alice"}
        code, _, _ = self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29", "--force")
        self.assertEqual((code, char_state(read(self.path), 0)[0]), (0, 29))

    def test_refuses_when_online_status_is_unknown(self):
        self.online = None
        code, _, err = self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29")
        self.assertEqual(code, 1)
        self.assertIn("API server unreachable", err)
        self.assertEqual(read(self.path), self.original)

    def test_all_characters(self):
        code, _, _ = self.run_cli("set-rank", "Alice", "--char", "all", "--rank", "3rd dan")
        after = read(self.path)
        self.assertEqual(code, 0)
        self.assertEqual({char_state(after, i) for i in range(ta.CHAR_N)}, {(12, 5000, 0)})
        self.assertEqual(after[ta.OFF_ACCOUNT_RANK], 12)

    def test_all_characters_takes_no_points(self):
        code, _, err = self.run_cli("set-rank", "Alice", "--char", "all", "--rank", "12", "--points", "1")
        self.assertEqual(code, 1)
        self.assertIn("--char all takes --rank", err)
        self.assertEqual(read(self.path), self.original)

    def test_character_out_of_range(self):
        code, _, err = self.run_cli("set-rank", "Alice", "--char", "59", "--rank", "12")
        self.assertEqual(code, 1)
        self.assertIn("--char must be 0..58 or all", err)

    def test_unknown_rank_name_is_a_usage_error(self):
        code, _, err = self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "platinum")
        self.assertEqual(code, 2)
        self.assertIn("unknown rank 'platinum'", err)

    def test_file_to_file_leaves_the_account_alone(self):
        out_path = os.path.join(self.root, "edited.tdt")
        code, _, _ = self.run_cli("set-rank", "--input-file", self.path, "--output-file", out_path,
                                  "--char", "0", "--rank", "29")
        self.assertEqual(code, 0)
        self.assertEqual(char_state(read(out_path), 0)[0], 29)
        self.assertTrue(sealed(read(out_path)))
        self.assertEqual(read(self.path), self.original)


class SetAccountRankTest(RpcnDataCase):
    def test_changes_only_the_account_rank(self):
        path = self.add_account("Alice", make_save(chars=[(0, 20, 1500)], account_rank=20))
        code, _, _ = self.run_cli("set-account-rank", "Alice", "--rank", "30")
        after = read(path)
        self.assertEqual(code, 0)
        self.assertEqual((after[ta.OFF_ACCOUNT_RANK], char_state(after, 0)), (30, (20, 1500, 0)))
        self.assertTrue(sealed(after))
        self.assertEqual(self.audit_records()[0]["action"], "set-account-rank")


class FloorTest(RpcnDataCase):
    def add_unfloored(self, name="Alice"):
        """reached Byakko with one character, everything else untouched"""
        buf = bytes(make_save(chars=[(0, 30, 100, 5, 5)], account_rank=30))
        return self.add_account(name, buf), buf

    def add_demoted(self, name="Alice"):
        """floored at 19 earlier, three characters demoted since"""
        chars = [(0, 30, 100), (1, 17, 0), (2, 17, 0), (3, 18, 0)]
        buf = bytes(make_save(every=(19, 5000), chars=chars, account_rank=30))
        return self.add_account(name, buf), buf

    def test_raises_characters_to_the_floor(self):
        path, _ = self.add_unfloored()
        code, out, _ = self.run_cli("floor", "Alice")
        after = read(path)
        self.assertEqual(code, 0)
        self.assertEqual(char_state(after, 0), (30, 100, 0))
        self.assertEqual({char_state(after, i) for i in range(1, ta.CHAR_N)}, {(19, 5000, 0)})
        self.assertTrue(sealed(after))
        self.assertIn("applied 1  no change 0", out)

    def test_audits_the_floor(self):
        self.add_unfloored()
        self.run_cli("floor", "Alice", "--label", "pre-test")
        (rec,) = self.audit_records()
        self.assertEqual((rec["action"], rec["floor"], rec["raised"]), ("floor", 19, 58))
        self.assertEqual(self.backups("Alice"), ["pre-test.tdt"])

    def test_several_accounts_add_a_batch_record(self):
        self.add_unfloored("Alice")
        self.add_unfloored("Bob")
        code, out, _ = self.run_cli("floor", "Alice", "Bob")
        self.assertEqual(code, 0)
        self.assertIn("applied 2", out)
        batch = self.audit_records()[-1]
        self.assertEqual((batch["action"], batch["accounts"], batch["targets"]), ("floor-batch", 2, 2))

    def test_all_covers_every_account_with_a_save(self):
        a, _ = self.add_unfloored("Alice")
        b, _ = self.add_unfloored("Bob")
        self.add_account("NoSave")
        code, out, _ = self.run_cli("floor", "--all")
        self.assertEqual(code, 0)
        self.assertIn("2 accounts, floor auto", out)
        self.assertEqual((char_state(read(a), 1)[0], char_state(read(b), 1)[0]), (19, 19))

    def test_explicit_rank(self):
        path, _ = self.add_unfloored()
        self.run_cli("floor", "Alice", "--rank", "3rd dan")
        self.assertEqual(char_state(read(path), 1), (12, 5000, 0))

    def test_dry_run_writes_nothing(self):
        path, original = self.add_unfloored()
        code, out, _ = self.run_cli("floor", "Alice", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("would apply 1", out)
        self.assertEqual((read(path), self.backups("Alice"), self.audit_records()), (original, [], []))

    def test_floored_account_is_left_alone(self):
        original = bytes(make_save(every=(10, 0), account_rank=10))
        path = self.add_account("Alice", original)
        code, out, _ = self.run_cli("floor", "Alice")
        self.assertEqual(code, 0)
        self.assertIn("applied 0  no change 1", out)
        self.assertEqual((read(path), self.backups("Alice")), (original, []))

    def test_skips_an_online_account(self):
        path, original = self.add_unfloored()
        self.online = {"Alice"}
        code, out, _ = self.run_cli("floor", "Alice")
        self.assertEqual(code, 0)
        self.assertIn("online 1", out)
        self.assertEqual(read(path), original)

    def test_stops_when_online_status_is_unknown(self):
        path, original = self.add_unfloored()
        self.online = None
        code, _, err = self.run_cli("floor", "Alice")
        self.assertEqual(code, 1)
        self.assertIn("API server unreachable", err)
        self.assertEqual(read(path), original)

    def test_keeps_demotions_after_an_earlier_floor(self):
        path, original = self.add_demoted()
        code, out, _ = self.run_cli("floor", "Alice")
        self.assertEqual(code, 0)
        self.assertIn("likely demoted 1", out)
        self.assertEqual(read(path), original)

    def test_refloor_undoes_demotions(self):
        path, _ = self.add_demoted()
        self.run_cli("floor", "Alice", "--refloor")
        after = read(path)
        self.assertEqual([char_state(after, i) for i in (1, 2, 3)], [(19, 5000, 0)] * 3)

    def test_fix_points_tops_up_characters_at_the_floor(self):
        buf = make_save(every=(19, 5000), chars=[(0, 30, 100), (5, 19, 100)], account_rank=30)
        path = self.add_account("Alice", buf)
        code, _, _ = self.run_cli("floor", "Alice", "--fix-points")
        self.assertEqual((code, char_state(read(path), 5)), (0, (19, 5000, 0)))
        self.assertEqual(self.audit_records()[0]["points_fixed"], 1)

    def test_rank_outside_the_floor_table(self):
        self.add_unfloored()
        code, _, err = self.run_cli("floor", "Alice", "--rank", "0")
        self.assertEqual(code, 1)
        self.assertIn("--rank must be 1..42", err)

    def test_one_unknown_name_stops_the_whole_run(self):
        path, original = self.add_unfloored()
        code, _, err = self.run_cli("floor", "Alice", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("1 of 2 accounts did not resolve; nothing was done", err)
        self.assertEqual(read(path), original)


class FloorRedoTest(RpcnDataCase):
    """a floor to 17 recorded under label pre-old, redone with the current rule"""

    def setUp(self):
        super().setUp()
        pre = make_save(chars=[(0, 30, 100, 5, 5)], account_rank=30)
        cur = make_save(every=(17, 3000), chars=[(0, 30, 100, 5, 5), (3, 18, 800, 2, 1)], account_rank=30)
        self.path = self.add_account("Alice", cur)
        backup = ta.backup_path("Alice", "pre-old")
        with open(backup, "wb") as f:
            f.write(bytes(pre))
        ta.audit("floor", "Alice", backup=backup, floor=17, raised=58)

    def test_moves_unplayed_characters_to_the_current_floor(self):
        code, out, _ = self.run_cli("floor", "--redo", "pre-old", "--label", "pre-redo")
        after = read(self.path)
        self.assertEqual(code, 0)
        self.assertIn("applied 1  played chars kept 1", out)
        self.assertEqual(char_state(after, 1), (19, 5000, 0))
        self.assertEqual(char_state(after, 3), (18, 800, 0))
        self.assertEqual(self.backups("Alice"), ["pre-old.tdt", "pre-redo.tdt"])

    def test_unknown_label(self):
        code, _, err = self.run_cli("floor", "--redo", "never")
        self.assertEqual(code, 1)
        self.assertIn("no floor recorded with backup label 'never'", err)

    def test_takes_no_floor_options(self):
        code, _, err = self.run_cli("floor", "--redo", "pre-old", "--refloor")
        self.assertEqual(code, 1)
        self.assertIn("--redo takes only npids", err)


class BackupRestoreTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.original = bytes(make_save(chars=[(0, 20, 1500, 10, 5)], account_rank=20))
        self.path = self.add_account("Alice", self.original)

    def test_backup_copies_the_save_under_the_label(self):
        code, _, _ = self.run_cli("backup", "Alice", "--label", "base")
        self.assertEqual(code, 0)
        self.assertEqual(read(os.path.join(self.backup_dir, "Alice", "base.tdt")), self.original)
        self.assertEqual(self.audit_records()[0]["action"], "backup")

    def test_backup_all_skips_nobody_with_a_save(self):
        self.add_account("Bob", make_save())
        self.add_account("NoSave")
        _, out, _ = self.run_cli("backup", "--all", "--label", "base")
        self.assertIn("2 backed up", out)

    def test_list_backups(self):
        self.run_cli("backup", "Alice", "--label", "base")
        code, out, _ = self.run_cli("list-backups", "Alice")
        self.assertEqual(code, 0)
        self.assertIn("base", out)
        self.assertIn("rank 20", out)

    def test_list_backups_with_none(self):
        _, out, _ = self.run_cli("list-backups")
        self.assertEqual(out, "no backups\n")

    def test_restore_by_label(self):
        self.run_cli("backup", "Alice", "--label", "base")
        self.run_cli("set-rank", "Alice", "--char", "0", "--rank", "29")
        code, _, _ = self.run_cli("restore", "Alice", "--label", "base")
        self.assertEqual((code, read(self.path)), (0, self.original))
        self.assertEqual(self.audit_records()[-1]["action"], "restore")

    def test_restore_without_a_backup(self):
        code, _, err = self.run_cli("restore", "Alice")
        self.assertEqual(code, 1)
        self.assertIn("no backup matching Alice/*.tdt", err)

    def test_restore_refuses_an_online_account(self):
        self.run_cli("backup", "Alice", "--label", "base")
        self.online = {"Alice"}
        code, _, err = self.run_cli("restore", "Alice", "--label", "base")
        self.assertEqual(code, 1)
        self.assertIn("Alice is online right now", err)

    def test_apply_writes_a_file_as_the_live_save(self):
        src = os.path.join(self.root, "new.tdt")
        buf = make_save(chars=[(7, 25, 7000)], account_rank=25)
        buf[0:4] = b"\0\0\0\0"   # apply seals it
        with open(src, "wb") as f:
            f.write(bytes(buf))
        code, _, _ = self.run_cli("apply", "Alice", "--file", src)
        after = read(self.path)
        self.assertEqual((code, char_state(after, 7), sealed(after)), (0, (25, 7000, 0), True))

    def test_apply_refuses_a_file_of_the_wrong_size(self):
        src = os.path.join(self.root, "short.tdt")
        with open(src, "wb") as f:
            f.write(b"\0" * 228)
        code, _, err = self.run_cli("apply", "Alice", "--file", src)
        self.assertEqual(code, 1)
        self.assertIn("expected 3420 bytes, got 228", err)
        self.assertEqual(read(self.path), self.original)


class LogTest(RpcnDataCase):
    def test_empty(self):
        _, out, _ = self.run_cli("log")
        self.assertEqual(out, "no audit log yet\n")

    def test_shows_the_last_entries(self):
        self.add_account("Alice", make_save())
        self.run_cli("set-account-rank", "Alice", "--rank", "11")
        self.run_cli("set-account-rank", "Alice", "--rank", "12")
        code, out, _ = self.run_cli("log", "-n", "1")
        self.assertEqual((code, len(out.splitlines())), (0, 1))
        self.assertIn("set-account-rank", out)
        self.assertIn("rank=12", out)


class GcTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.live = self.add_account("Alice", make_save())
        self.old = self.orphan_file(500, make_save(), age_days=30)
        self.recent = self.orphan_file(501, make_save(), age_days=1)

    def test_dry_run_only_reports(self):
        code, out, _ = self.run_cli("gc")
        self.assertEqual(code, 0)
        self.assertIn("removable orphans  1", out)
        self.assertTrue(os.path.exists(self.old))

    def test_apply_removes_old_orphans_only(self):
        code, _, _ = self.run_cli("gc", "--apply")
        self.assertEqual(code, 0)
        self.assertEqual([os.path.exists(p) for p in (self.live, self.old, self.recent)], [True, False, True])
        self.assertEqual((self.audit_records()[-1]["action"], self.audit_records()[-1]["removed"]), ("gc", 1))


if __name__ == "__main__":
    unittest.main()
