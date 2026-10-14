"""Character and rank names come from tag2now-BE, with a snapshot for when it is down."""
import io
import os
import json
import argparse
import tempfile
import threading
import contextlib
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import tdt_admin as ta
from tests.support import GAME_DATA, real_fetch_game_data, reset_game_data


class GameDataCase(unittest.TestCase):
    """a fresh cache, a backup directory to hold the snapshot, and a BE that answers
    whatever self.answer is (an exception is raised instead of answered)"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.backup_dir = tmp.name
        self.answer = GAME_DATA
        self.fetches = 0
        self.now = 1000.0
        self.err = io.StringIO()

        def fetch():
            self.fetches += 1
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        for p in (mock.patch.object(ta, "BACKUP_DIR", self.backup_dir),
                  mock.patch.object(ta, "fetch_game_data", side_effect=fetch),
                  mock.patch.object(ta.time, "monotonic", side_effect=lambda: self.now),
                  contextlib.redirect_stderr(self.err)):
            p.__enter__()
            self.addCleanup(p.__exit__, None, None, None)
        reset_game_data()
        self.addCleanup(reset_game_data)

    def snapshot(self):
        with open(ta.snapshot_path(), encoding="utf-8") as f:
            return json.load(f)

    def write_snapshot(self, data):
        with open(ta.snapshot_path(), "w", encoding="utf-8") as f:
            json.dump({"saved_at": "2026-10-01T12:00:00", **data}, f)


class FetchTest(GameDataCase):
    def test_reads_names_from_be(self):
        self.assertEqual(ta.char_name(0x1B), "Devil Jin")
        self.assertEqual(ta.rank_name(30), ("Byakko", "빨강단"))
        self.assertTrue(ta.game_data().fresh)

    def test_keeps_what_it_fetched_as_the_snapshot(self):
        ta.game_data()
        snap = self.snapshot()
        self.assertEqual((snap["characters"], snap["ranks"]), (GAME_DATA["characters"], GAME_DATA["ranks"]))

    def test_asks_again_only_after_the_ttl(self):
        ta.char_name(0)
        self.now += ta.GAME_DATA_TTL - 1
        ta.char_name(0)
        self.assertEqual(self.fetches, 1)
        self.now += 1
        ta.char_name(0)
        self.assertEqual(self.fetches, 2)

    def test_takes_a_new_name_on_the_next_fetch(self):
        ta.char_name(0)
        renamed = json.loads(json.dumps(GAME_DATA))
        renamed["characters"][0x33]["name"] = "Angel"
        self.answer = renamed
        self.now += ta.GAME_DATA_TTL
        self.assertEqual(ta.char_name(0x33), "Angel")


class UnreachableTest(GameDataCase):
    def setUp(self):
        super().setUp()
        self.answer = OSError("connection refused")

    def test_falls_back_to_the_snapshot_and_says_so(self):
        self.write_snapshot(GAME_DATA)
        self.assertEqual(ta.char_name(0), "Paul")
        self.assertFalse(ta.game_data().fresh)
        self.assertIn("snapshot of 2026-10-01T12:00:00", self.err.getvalue())

    def test_shows_ids_when_there_is_no_snapshot(self):
        self.assertEqual(ta.char_name(0x33), "Unknown (0x33)")
        self.assertEqual(ta.rank_name(30), ("Unknown (30)", "Unknown"))

    def test_keeps_the_names_it_had_but_stops_trusting_them(self):
        self.answer = GAME_DATA
        ta.game_data()
        self.answer = OSError("connection refused")
        self.now += ta.GAME_DATA_TTL
        self.assertEqual(ta.char_name(0), "Paul")
        self.assertFalse(ta.game_data().fresh)

    def test_retries_sooner_than_the_ttl(self):
        ta.game_data()
        self.now += ta.GAME_DATA_RETRY
        self.answer = GAME_DATA
        self.assertTrue(ta.game_data().fresh)

    def test_warns_once(self):
        for _ in range(3):
            self.now += ta.GAME_DATA_RETRY
            ta.char_name(0)
        self.assertEqual(self.err.getvalue().count("warn:"), 1)

    def test_numbers_still_parse(self):
        self.assertEqual((ta.parse_char("5"), ta.parse_char("all"), ta.parse_rank("29")), (5, ta.ALL_CHARS, 29))

    def test_refuses_names_even_with_a_snapshot(self):
        # an old table could map the name to the wrong slot, and a write would land there
        self.write_snapshot(GAME_DATA)
        for parse, name in ((ta.parse_char, "paul"), (ta.parse_rank, "genbu")):
            with self.assertRaisesRegex(argparse.ArgumentTypeError, "use the number"):
                parse(name)


class BadAnswerTest(GameDataCase):
    def test_a_short_table_leaves_the_snapshot_alone(self):
        self.write_snapshot(GAME_DATA)
        short = {"characters": GAME_DATA["characters"][:-1], "ranks": GAME_DATA["ranks"]}
        self.answer = short
        self.assertFalse(ta.game_data().fresh)
        self.assertEqual(len(self.snapshot()["characters"]), ta.CHAR_N)

    def test_an_empty_name_is_refused(self):
        blank = json.loads(json.dumps(GAME_DATA))
        blank["ranks"][3]["name"] = " "
        with self.assertRaisesRegex(ValueError, "empty name"):
            ta.parse_game_data(blank, True, "test")

    def test_not_json_shaped_like_the_table_is_refused(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            ta.parse_game_data({"characters": [{"id": 0}]}, True, "test")

    def test_no_backup_directory_means_no_snapshot(self):
        with mock.patch.object(ta, "BACKUP_DIR", os.path.join(self.backup_dir, "missing")):
            self.assertTrue(ta.game_data().fresh)
            self.assertFalse(os.path.exists(ta.snapshot_path()))


class FetchOverHttpTest(unittest.TestCase):
    def test_gets_game_data_from_be(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = json.dumps(GAME_DATA).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        with mock.patch.object(ta, "TAG2NOW_API_URL", f"http://127.0.0.1:{server.server_port}"):
            self.assertEqual(real_fetch_game_data(), GAME_DATA)
        self.assertEqual(seen, ["/game-data"])


if __name__ == "__main__":
    unittest.main()
