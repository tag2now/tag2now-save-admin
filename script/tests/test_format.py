"""The save format and the floor rule: checksum, decode, floor, redo, name parsing."""
import argparse
import unittest

import tdt_admin as ta
from tests.support import char_state, make_save, put_char, slot


class ChecksumTest(unittest.TestCase):
    def test_known_value(self):
        # the same code validates real saves written by the game; this pins it
        buf = bytearray(i * 7 % 251 for i in range(ta.REC))
        self.assertEqual(ta.checksum(buf), 0x300571A3)

    def test_ignores_the_stored_checksum(self):
        buf = bytearray(i * 7 % 251 for i in range(ta.REC))
        buf[0:4] = b"\xff\xff\xff\xff"
        self.assertEqual(ta.checksum(buf), 0x300571A3)

    def test_changes_with_any_data_byte(self):
        buf = make_save(account_rank=5)
        before = ta.checksum(buf)
        buf[ta.REC - 1] ^= 1
        self.assertNotEqual(ta.checksum(buf), before)

    def test_reseal_stores_the_checksum_big_endian(self):
        buf = bytearray(i * 7 % 251 for i in range(ta.REC))
        self.assertEqual(ta.reseal(buf), 0x300571A3)
        self.assertEqual(bytes(buf[0:4]), bytes.fromhex("300571A3"))


class DecodeTest(unittest.TestCase):
    def test_reads_account_and_character_fields(self):
        buf = make_save(chars=[(0, 30, 1234, 10, 5, 3)], account_rank=31)
        buf[ta.OFF_ACCOUNT_PROGRESS] = 7
        buf[ta.OFF_TOTAL:ta.OFF_TOTAL + 4] = (15).to_bytes(4, "big")
        buf[ta.OFF_WINS:ta.OFF_WINS + 4] = (10).to_bytes(4, "big")
        buf[ta.OFF_LOSSES:ta.OFF_LOSSES + 4] = (5).to_bytes(4, "big")
        d = ta.decode(buf)
        self.assertEqual((d["account_rank"], d["progress"], d["total"], d["wins"], d["losses"]),
                         (31, 7, 15, 10, 5))
        self.assertEqual(d["chars"], [{
            "id": 0, "character": "Paul", "rank": 30, "rank_name": "Byakko", "tier": "빨강단",
            "points": 1234, "streak": 3, "wins": 10, "losses": 5}])

    def test_streak_is_signed(self):
        buf = make_save(chars=[(4, 12, 0, 1, 1, -2)])
        self.assertEqual(ta.decode(buf)["chars"][0]["streak"], -2)

    def test_lists_only_used_characters_unless_asked(self):
        buf = make_save(chars=[(4, 12, 0, 1, 1)])
        self.assertEqual([c["id"] for c in ta.decode(buf)["chars"]], [4])
        self.assertEqual(len(ta.decode(buf, all_chars=True)["chars"]), ta.CHAR_N)

    def test_unknown_rank_code_is_named_as_such(self):
        self.assertEqual(ta.rank_name(99), ("Unknown (99)", "Unknown"))


class FloorRuleTest(unittest.TestCase):
    def test_floor_by_reached_rank(self):
        expected = {0: 10, 9: 10, 10: 10, 12: 10, 13: 10, 16: 10, 17: 12, 20: 12, 21: 14, 24: 14,
                    25: 17, 28: 17, 29: 19, 32: 19, 33: 21, 37: 21, 38: 29, 40: 29, 41: 33, 42: 33}
        self.assertEqual({m: ta.floor_for(m) for m in expected}, expected)

    def test_floor_points(self):
        expected = {1: 200, 5: 1000, 9: 1800, 10: 0, 11: 5000, 24: 5000, 25: 7000, 42: 7000}
        self.assertEqual({r: ta.FLOOR_POINTS[r] for r in expected}, expected)

    def test_reached_counts_the_account_rank(self):
        # characters are demoted, the account rank is not
        buf = make_save(chars=[(0, 20)], account_rank=30)
        self.assertEqual(ta.reached(buf), 30)

    def test_likely_demotions_is_a_few_characters_below_the_floor(self):
        self.assertEqual([ta.likely_demotions(n) for n in (0, 1, ta.RESTORE_MAX, ta.RESTORE_MAX + 1)],
                         [False, True, True, False])


class FloorBufTest(unittest.TestCase):
    def test_raises_everything_below_the_floor(self):
        buf = make_save(chars=[(0, 30, 100), (1, 25, 300)], account_rank=12)
        self.assertEqual(ta.floor_buf(buf), (30, 19, 58))   # 57 characters and the account rank
        self.assertEqual(char_state(buf, 0), (30, 100, 0))
        self.assertEqual(char_state(buf, 1), (25, 300, 0))
        self.assertEqual(char_state(buf, 2), (19, 5000, 0))
        self.assertEqual(buf[ta.OFF_ACCOUNT_RANK], 19)

    def test_explicit_rank_overrides_the_rule(self):
        buf = make_save(chars=[(0, 30)], account_rank=30)
        self.assertEqual(ta.floor_buf(buf, 12), (30, 12, 58))
        self.assertEqual(char_state(buf, 58), (12, 5000, 0))

    def test_leaves_a_floored_save_alone(self):
        buf = make_save(every=(10, 0), account_rank=10)
        before = bytes(buf)
        self.assertEqual(ta.floor_buf(buf), (10, 10, 0))
        self.assertEqual(bytes(buf), before)

    def test_keeps_the_streak_of_a_raised_character(self):
        buf = make_save(chars=[(0, 30), (1, 5, 0, 2, 9, -4)], account_rank=30)
        ta.floor_buf(buf)
        self.assertEqual(char_state(buf, 1), (19, 5000, 0xFC))

    def test_fix_floor_points_tops_up_characters_at_the_floor(self):
        buf = make_save(every=(19, 5000), chars=[(0, 30, 10), (5, 19, 100), (6, 20, 100)], account_rank=30)
        self.assertEqual(ta.fix_floor_points(buf, 19), 1)
        self.assertEqual(char_state(buf, 5), (19, 5000, 0))
        self.assertEqual(char_state(buf, 6), (20, 100, 0))


class SetAllBufTest(unittest.TestCase):
    def test_sets_every_character_and_the_account_rank(self):
        buf = make_save(chars=[(0, 30, 100, 1, 1, 3), (1, 12, 5000)], account_rank=30)
        self.assertEqual(ta.set_all_buf(buf, 12), 58)   # character 1 is already there
        self.assertEqual({char_state(buf, i) for i in range(ta.CHAR_N)}, {(12, 5000, 0)})
        self.assertEqual(buf[ta.OFF_ACCOUNT_RANK], 12)


class RedoBufTest(unittest.TestCase):
    """an earlier floor to 17 (3000 points), redone with the current rule (19, 5000 points)"""

    def setUp(self):
        self.pre = make_save(chars=[(0, 30, 100, 5, 5)], account_rank=30)
        self.cur = bytearray(self.pre)
        for i in range(1, ta.CHAR_N):
            put_char(self.cur, i, 17, 3000)

    def test_moves_untouched_characters_to_the_current_floor(self):
        y_new, changes, played = ta.redo_buf(self.pre, self.cur, 17)
        self.assertEqual((y_new, len(changes), played), (19, 58, []))
        self.assertEqual(changes[0], (1, (17, 3000, 0), (19, 5000, 0)))
        self.assertEqual(char_state(self.cur, 58), (19, 5000, 0))
        self.assertEqual(char_state(self.cur, 0), (30, 100, 0))

    def test_keeps_characters_played_since(self):
        put_char(self.cur, 3, 18, 800, wins=2, losses=1)
        _, changes, played = ta.redo_buf(self.pre, self.cur, 17)
        self.assertEqual((len(changes), played), (57, [3]))
        self.assertEqual(char_state(self.cur, 3), (18, 800, 0))

    def test_second_redo_changes_nothing(self):
        ta.redo_buf(self.pre, self.cur, 17)
        after = bytes(self.cur)
        _, changes, played = ta.redo_buf(self.pre, self.cur, 17)
        self.assertEqual((changes, played, bytes(self.cur)), ([], [], after))

    def test_moves_an_account_rank_the_earlier_floor_raised(self):
        self.pre[ta.OFF_ACCOUNT_RANK] = 12
        self.cur[ta.OFF_ACCOUNT_RANK] = 17
        _, changes, _ = ta.redo_buf(self.pre, self.cur, 17)
        self.assertEqual(changes[-1], ("account", (17,), (19,)))
        self.assertEqual(self.cur[ta.OFF_ACCOUNT_RANK], 19)

    def test_does_not_touch_the_record(self):
        ta.redo_buf(self.pre, self.cur, 17)
        o = slot(0)
        self.assertEqual(self.cur[o + ta.SLOT_WIN:o + ta.SLOT_LOSS + 4],
                         self.pre[o + ta.SLOT_WIN:o + ta.SLOT_LOSS + 4])


class ParseTest(unittest.TestCase):
    def test_rank_by_number_or_name(self):
        given = ["29", "Genbu", "tekken god", "1st dan", "True-Tekken-God"]
        self.assertEqual([ta.parse_rank(s) for s in given], [29, 29, 41, 10, 42])

    def test_unknown_rank(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            ta.parse_rank("platinum")

    def test_character_by_number_or_name(self):
        given = ["5", "paul", "Devil Jin", "p-jack", "all"]
        self.assertEqual([ta.parse_char(s) for s in given], [5, 0, 0x1B, 0x31, ta.ALL_CHARS])

    def test_character_name_on_two_slots_is_refused(self):
        with self.assertRaises(argparse.ArgumentTypeError) as cm:
            ta.parse_char("michelle")
        self.assertIn("ambiguous", str(cm.exception))

    def test_unknown_character(self):
        with self.assertRaises(argparse.ArgumentTypeError):
            ta.parse_char("akuma")


if __name__ == "__main__":
    unittest.main()
