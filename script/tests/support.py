"""Fixtures for the tdt_admin tests: synthetic saves and a throwaway RPCN data directory.

Run from script/, on Linux (the tool writes through /tmp and targets the RPCN host):

    python3 -m unittest discover -s tests -t . -v
"""
import io
import os
import sys
import json
import time
import sqlite3
import tempfile
import unittest
import contextlib
from unittest import mock

import tdt_admin as ta

# RPCN stores timestamps as microseconds since 0001-01-01
TICK_EPOCH = 62135596800


def slot(i):
    return ta.CHAR_BASE + i * ta.CHAR_STRIDE


def put_char(b, i, rank, points=0, wins=0, losses=0, streak=0):
    o = slot(i)
    b[o + ta.SLOT_RANK] = rank
    b[o + ta.SLOT_POINTS:o + ta.SLOT_POINTS + 2] = points.to_bytes(2, "big")
    b[o + ta.SLOT_STREAK] = streak & 0xFF
    b[o + ta.SLOT_WIN:o + ta.SLOT_WIN + 4] = wins.to_bytes(4, "big")
    b[o + ta.SLOT_LOSS:o + ta.SLOT_LOSS + 4] = losses.to_bytes(4, "big")


def make_save(chars=(), account_rank=0, every=None):
    """a sealed save; every=(rank, points) fills all 59 characters first,
    then chars=[(id, rank, points, wins, losses, streak), ...] overrides"""
    b = bytearray(ta.REC)
    if every is not None:
        for i in range(ta.CHAR_N):
            put_char(b, i, *every)
    for args in chars:
        put_char(b, *args)
    b[ta.OFF_ACCOUNT_RANK] = account_rank
    ta.reseal(b)
    return b


def char_state(b, i):
    """(rank, points, streak byte) of character i"""
    o = slot(i)
    return b[o], ta.be16(b, o + ta.SLOT_POINTS), b[o + ta.SLOT_STREAK]


class RpcnDataCase(unittest.TestCase):
    """points tdt_admin at a temp database, save directory and backup directory"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.db_path = os.path.join(self.root, "rpcn.db")
        self.tus_dir = os.path.join(self.root, "tus_data")
        self.backup_dir = os.path.join(self.root, "backup", "tdt")
        self.audit_log = os.path.join(self.backup_dir, "audit.jsonl")
        os.makedirs(self.tus_dir)
        self._create_db()

        # None means the API server cannot be reached
        self.online = set()
        patches = [
            mock.patch.multiple(
                ta, DB_PATH=self.db_path, TUS_DIR=self.tus_dir, BACKUP_DIR=self.backup_dir,
                ARCHIVE_DIR=os.path.join(self.root, "backup", "tdt_archive"), AUDIT_LOG=self.audit_log),
            mock.patch.object(ta, "online", side_effect=lambda: self.online),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self._next_id = 1

    def _create_db(self):
        con = sqlite3.connect(self.db_path)
        con.executescript(
            "CREATE TABLE account ( user_id INTEGER PRIMARY KEY, username TEXT NOT NULL );"
            "CREATE TABLE account_timestamp ( user_id INTEGER PRIMARY KEY, creation INTEGER NOT NULL );"
            "CREATE TABLE tus_data ( owner_id UNSIGNED BIGINT NOT NULL, communication_id TEXT NOT NULL, "
            "slot_id INTEGER NOT NULL, data_id UNSIGNED BIGINT NOT NULL, data_info BLOB NOT NULL, "
            "timestamp UNSIGNED BIGINT NOT NULL, author_id UNSIGNED BIGINT NOT NULL, "
            "PRIMARY KEY (owner_id, communication_id, slot_id) );"
            "CREATE TABLE tus_data_vuser ( vuser TEXT NOT NULL, communication_id TEXT NOT NULL, "
            "slot_id INTEGER NOT NULL, data_id UNSIGNED BIGINT NOT NULL, data_info BLOB NOT NULL, "
            "timestamp UNSIGNED BIGINT NOT NULL, author_id UNSIGNED BIGINT NOT NULL, "
            "PRIMARY KEY (vuser, communication_id, slot_id) );")
        con.close()

    def add_account(self, username, buf=None, created=0):
        """an account created at unix time `created`, with a TTT2 save when buf is given;
        returns the save path"""
        uid = self._next_id
        self._next_id += 1
        con = sqlite3.connect(self.db_path)
        with con:
            con.execute("INSERT INTO account (user_id, username) VALUES (?, ?)", (uid, username))
            con.execute("INSERT INTO account_timestamp (user_id, creation) VALUES (?, ?)", (uid, created))
        path = None
        if buf is not None:
            data_id = 1000 + uid
            tick = (int(time.time()) + TICK_EPOCH) * 1_000_000
            with con:
                con.execute(
                    "INSERT INTO tus_data (owner_id, communication_id, slot_id, data_id, data_info, "
                    "timestamp, author_id) VALUES (?, ?, ?, ?, x'', ?, ?)",
                    (uid, ta.COM_ID.encode(), ta.SLOT, data_id, tick, uid))
            path = self.orphan_file(data_id, buf)
        con.close()
        return path

    def orphan_file(self, data_id, buf, age_days=0):
        """a save file on disk that no database row points at"""
        path = os.path.join(self.tus_dir, f"{data_id:020d}.tdt")
        with open(path, "wb") as f:
            f.write(bytes(buf))
        then = time.time() - age_days * 86400
        os.utime(path, (then, then))
        return path

    def run_cli(self, *argv):
        """run tdt_admin.py with argv; returns (exit code, stdout, stderr)"""
        out, err = io.StringIO(), io.StringIO()
        code = 0
        with mock.patch.object(sys, "argv", ["tdt_admin.py", *argv]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                ta.main()
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def backups(self, npid):
        d = os.path.join(self.backup_dir, npid)
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def audit_records(self):
        if not os.path.exists(self.audit_log):
            return []
        with open(self.audit_log, encoding="utf-8") as f:
            return [json.loads(line) for line in f]


def read(path):
    with open(path, "rb") as f:
        return f.read()
