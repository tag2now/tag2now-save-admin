"""The functions other programs call: they raise TdtError and return what they did."""
import os
import fcntl
import hashlib
import threading
import unittest

import tdt_admin as ta
from tests.support import RpcnDataCase, char_state, make_save, read


def sha256(data):
    return hashlib.sha256(data).hexdigest()


class ApplySaveTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.original = bytes(make_save(chars=[(0, 20, 1500, 10, 5)], account_rank=20))
        self.path = self.add_account("Alice", self.original)
        self.edited = bytearray(self.original)
        ta.set_rank_buf(self.edited, 0, 29)

    def test_returns_what_it_wrote(self):
        r = ta.apply_save("Alice", self.edited, "set-rank")
        after = read(self.path)
        self.assertEqual(char_state(after, 0), (29, 1500, 0))
        self.assertEqual((r["data_id"], r["data_id_now"], r["landed"]), (1001, 1001, True))
        self.assertEqual(r["checksum"], ta.be32(after, 0))
        self.assertEqual(read(r["backup"]), self.original)

    def test_audits_the_named_actor(self):
        ta.apply_save("Alice", self.edited, "set-rank", actor="admin1", rank=29)
        (rec,) = self.audit_records()
        self.assertEqual((rec["user"], rec["action"], rec["rank"]), ("admin1", "set-rank", 29))

    def test_writes_when_the_save_is_the_one_expected(self):
        ta.apply_save("Alice", self.edited, "set-rank", expect_sha256=sha256(self.original))
        self.assertEqual(char_state(read(self.path), 0)[0], 29)

    def test_leaves_a_save_that_changed_since_it_was_read(self):
        with self.assertRaises(ta.TdtError) as cm:
            ta.apply_save("Alice", self.edited, "set-rank", expect_sha256=sha256(b"something else"))
        self.assertIn("changed since it was read", str(cm.exception))
        self.assertEqual((read(self.path), self.backups("Alice"), self.audit_records()), (self.original, [], []))

    def test_online_account_raises(self):
        self.online = {"Alice"}
        with self.assertRaises(ta.TdtError):
            ta.apply_save("Alice", self.edited, "set-rank")
        self.assertEqual(read(self.path), self.original)

    def test_waits_for_another_writer_of_the_same_account(self):
        writer = threading.Thread(target=ta.apply_save, args=("Alice", self.edited, "set-rank"))
        with open(ta.lock_path("Alice"), "w") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            writer.start()
            writer.join(0.3)
            self.assertTrue(writer.is_alive())
            self.assertEqual(read(self.path), self.original)
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(char_state(read(self.path), 0)[0], 29)

    def test_lock_files_stay_out_of_the_backup_listing(self):
        ta.apply_save("Alice", self.edited, "set-rank", label="base")
        self.assertEqual(ta.list_backups(), [{"npid": "Alice", "label": "base", "total": 0, "account_rank": 20}])


class ReadingTest(RpcnDataCase):
    def setUp(self):
        super().setUp()
        self.original = bytes(make_save(chars=[(0, 20, 1500, 10, 5)], account_rank=20))
        self.path = self.add_account("Alice", self.original)

    def test_show_save(self):
        d = ta.show_save("Alice")
        self.assertEqual((d["npid"], d["data_id"], d["file"]), ("Alice", 1001, self.path))
        self.assertEqual((d["sha256"], d["checksum_ok"]), (sha256(self.original), True))
        self.assertEqual([c["id"] for c in d["chars"]], [0])

    def test_show_save_of_an_account_without_one_raises(self):
        self.add_account("Bob")
        with self.assertRaises(ta.TdtError):
            ta.show_save("Bob")

    def test_resolve_raises_for_an_unknown_account(self):
        with self.assertRaises(ta.TdtError) as cm:
            ta.resolve("nobody")
        self.assertEqual(str(cm.exception), "account 'nobody' not found")

    def test_list_backups_marks_a_file_of_the_wrong_size(self):
        with open(ta.backup_path("Alice", "short"), "wb") as f:
            f.write(b"\0" * 228)
        self.assertEqual(ta.list_backups("Alice"),
                         [{"npid": "Alice", "label": "short", "total": None, "account_rank": None}])

    def test_find_backup_takes_the_latest_by_name(self):
        for label in ("20260101-000000", "20260301-000000", "20260201-000000"):
            ta.take_backup("Alice", self.path, label)
        self.assertEqual(os.path.basename(ta.find_backup("Alice")), "20260301-000000.tdt")
        self.assertEqual(os.path.basename(ta.find_backup("Alice", "20260101-000000")), "20260101-000000.tdt")

    def test_find_backup_without_one_raises(self):
        with self.assertRaises(ta.TdtError):
            ta.find_backup("Alice")

    def test_read_log(self):
        self.assertEqual(ta.read_log(5), [])
        for n in (1, 2, 3):
            ta.audit("note", "Alice", n=n)
        self.assertEqual([r["n"] for r in ta.read_log(2)], [2, 3])


class RankEditTest(unittest.TestCase):
    def test_accepts_what_can_be_written(self):
        for args in [(0, 0), (58, 255, 65535), (ta.ALL_CHARS, 1), (ta.ALL_CHARS, 42)]:
            ta.check_rank_edit(*args)

    def test_refuses_the_rest(self):
        for args in [(59, 10), (-2, 10), (0, 256), (0, 10, 65536), (ta.ALL_CHARS, 0), (ta.ALL_CHARS, 12, 100)]:
            with self.assertRaises(ta.TdtError, msg=args):
                ta.check_rank_edit(*args)

    def test_set_rank_buf_returns_the_old_state(self):
        buf = make_save(chars=[(3, 20, 1500)])
        self.assertEqual(ta.set_rank_buf(buf, 3, 29, 3000), (20, 1500))
        self.assertEqual(char_state(buf, 3), (29, 3000, 0))


if __name__ == "__main__":
    unittest.main()
