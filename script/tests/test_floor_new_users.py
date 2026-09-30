"""floor_new_users.py: accounts created after the cutoff start at the floor."""
import io
import os
import sys
import contextlib
import unittest
from unittest import mock

import tdt_admin as ta
import floor_new_users as fn
from tests.support import RpcnDataCase, char_state, make_save, read

AFTER = int(fn.CUTOFF.timestamp()) + 100
BEFORE = int(fn.CUTOFF.timestamp()) - 100


class FloorNewUsersTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.state_path = os.path.join(self.root, "floor_new_users.json")
        self.template = os.path.join(self.root, "template_new_user.tdt")
        with open(self.template, "wb") as f:
            f.write(bytes(make_save()))
        p = mock.patch.multiple(fn, DB_PATH=self.db_path, BACKUP_DIR=self.backup_dir,
                                STATE=self.state_path, TEMPLATE=self.template)
        p.start()
        self.addCleanup(p.stop)
        self.state = {"cutoff": fn.CUTOFF.isoformat(), "done": {}}
        self.unfloored = bytes(make_save(chars=[(0, 30, 100, 5, 5)], account_rank=30))

    def run_once(self, dry_run=False):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            fn.run_once(self.state, dry_run)
        return out.getvalue()

    def test_floors_a_new_user_and_marks_it_done(self):
        path = self.add_account("New", self.unfloored, created=AFTER)
        self.run_once()
        self.assertEqual(char_state(read(path), 1), (19, 5000, 0))
        self.assertEqual(self.state["done"]["New"]["status"], "applied")
        self.assertEqual(fn.load_state()["done"].keys(), {"New"})

    def test_ignores_accounts_created_before_the_cutoff(self):
        path = self.add_account("Old", self.unfloored, created=BEFORE)
        self.run_once()
        self.assertEqual((read(path), self.state["done"]), (self.unfloored, {}))

    def test_leaves_an_online_user_for_a_later_run(self):
        path = self.add_account("New", self.unfloored, created=AFTER)
        self.online = {"New"}
        self.run_once()
        self.assertEqual((read(path), self.state["done"]), (self.unfloored, {}))

    def test_does_nothing_when_online_status_is_unknown(self):
        path = self.add_account("New", self.unfloored, created=AFTER)
        self.online = None
        out = self.run_once()
        self.assertIn("stat server unreachable", out)
        self.assertEqual((read(path), self.state["done"]), (self.unfloored, {}))

    def test_dry_run_writes_nothing(self):
        path = self.add_account("New", self.unfloored, created=AFTER)
        out = self.run_once(dry_run=True)
        self.assertIn("New (created", out)
        self.assertEqual((read(path), self.state["done"]), (self.unfloored, {}))
        self.assertFalse(os.path.exists(self.state_path))

    def test_seeds_a_floored_save_for_a_user_without_one(self):
        self.add_account("Old", self.unfloored, created=BEFORE)
        self.add_account("Fresh", created=AFTER)
        out = self.run_once()
        self.assertIn("Fresh (created", out)
        _, _, _, path = ta.lookup("Fresh")
        seeded = read(path)
        self.assertEqual({char_state(seeded, i) for i in range(ta.CHAR_N)}, {(10, 0, 0)})
        self.assertEqual(ta.checksum(seeded), ta.be32(seeded, 0))
        # not done yet: a later run floors whatever the game saved in the meantime
        self.assertEqual(self.state["done"], {})
        self.assertEqual(self.audit_records()[-1]["action"], "seed-floor-save")

    def test_missing_template_is_an_error_exit(self):
        self.add_account("Old", self.unfloored, created=BEFORE)
        self.add_account("Fresh", created=AFTER)
        os.unlink(self.template)
        fn.save_state(self.state)
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["floor_new_users.py"]), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit) as cm:
            fn.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("save file missing", err.getvalue())


if __name__ == "__main__":
    unittest.main()
