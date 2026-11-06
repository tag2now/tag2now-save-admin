"""template_new_user.tdt: the save every new account is seeded from."""
import unittest

import tdt_admin as ta
import floor_new_users as fn


class TemplateTest(unittest.TestCase):
    def test_is_a_sealed_fresh_save(self):
        b = ta.read_save(fn.TEMPLATE)
        d = ta.decode(b, all_chars=True)
        self.assertEqual(ta.checksum(b), d["checksum"])
        self.assertEqual((d["account_rank"], d["total"], d["wins"], d["losses"]), (0, 0, 0, 0))
        self.assertEqual({(c["rank"], c["points"], c["wins"], c["losses"]) for c in d["chars"]},
                         {(0, 0, 0, 0)})


if __name__ == "__main__":
    unittest.main()
