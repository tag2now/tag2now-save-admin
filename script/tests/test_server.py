"""tdt_admin_server.py: the HTTP front tag2now-BE calls for the admin page."""
import json
import threading
import urllib.error
import urllib.request
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import tdt_admin as ta
import tdt_admin_server as srv
from tests.support import RpcnDataCase, char_state, make_save, read

KEY = "server-key"


def setUpModule():
    # the server logs refusals and failures by design; the tests provoke them
    srv.log.disabled = True


def tearDownModule():
    srv.log.disabled = False


class ServerCase(RpcnDataCase):
    """handle() against a temp RPCN data directory, with every admin accepted"""

    def setUp(self):
        super().setUp()
        p = mock.patch.object(srv, "verify_admin")
        self.verify_admin = p.start()
        self.addCleanup(p.stop)

    def call(self, path, **body):
        body.setdefault("admin_username", "Admin")
        body.setdefault("admin_password", "DERIVED")
        return srv.handle(KEY, path, KEY, json.dumps(body).encode())

    def ok(self, path, **body):
        status, out = self.call(path, **body)
        self.assertEqual(status, 200, out)
        return out

    def fails(self, path, status, code, **body):
        got, out = self.call(path, **body)
        self.assertEqual((got, out["error"]), (status, code), out)
        return out["message"]


class GatewayTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.add_account("Alice", make_save())

    def test_wrong_or_missing_api_key(self):
        for given in ("wrong", "", None):
            status, out = srv.handle(KEY, "/saves/show", given, b"{}")
            self.assertEqual((status, out["error"]), (403, "forbidden"))
        self.verify_admin.assert_not_called()

    def test_unknown_route(self):
        status, out = srv.handle(KEY, "/saves/delete", KEY, b"{}")
        self.assertEqual((status, out["error"]), (404, "not_found"))

    def test_body_must_be_a_json_object(self):
        for raw in (b"not json", b"[1, 2]", b"\xff"):
            status, out = srv.handle(KEY, "/saves/show", KEY, raw)
            self.assertEqual((status, out["error"]), (400, "invalid_request"), raw)

    def test_admin_credentials_are_required(self):
        msg = self.fails("/saves/show", 400, "invalid_request", username="Alice", admin_password="")
        self.assertEqual(msg, "admin_password is required")

    def test_checks_the_admin_before_anything_else(self):
        self.ok("/saves/show", username="Alice", admin_username="Admin", admin_password="DERIVED")
        self.verify_admin.assert_called_once_with("Admin", "DERIVED")

    def test_rejected_admin_reaches_no_route(self):
        self.verify_admin.side_effect = ta.TdtError("not an admin", "forbidden")
        with mock.patch.object(ta, "show_save") as show:
            self.fails("/saves/show", 403, "forbidden", username="Alice")
        show.assert_not_called()

    def test_unexpected_failure_is_a_500_without_details(self):
        with mock.patch.object(ta, "show_save", side_effect=RuntimeError("secret detail")):
            msg = self.fails("/saves/show", 500, "internal_error", username="Alice")
        self.assertNotIn("secret", msg)


class ReadRoutesTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.original = bytes(make_save(chars=[(0, 20, 1500, 10, 5)], account_rank=20))
        self.path = self.add_account("Alice", self.original)

    def test_show(self):
        out = self.ok("/saves/show", username="alice")
        self.assertEqual((out["npid"], out["data_id"], out["online"], out["checksum_ok"]), ("Alice", 1001, False, True))
        self.assertEqual(out["sha256"], srv.sha256(self.original))
        self.assertEqual(len(self.ok("/saves/show", username="Alice", all_chars=True)["chars"]), ta.CHAR_N)

    def test_show_while_online_status_is_unknown(self):
        self.online = None
        self.assertIsNone(self.ok("/saves/show", username="Alice")["online"])

    def test_unknown_account(self):
        self.fails("/saves/show", 404, "user_not_found", username="nobody")

    def test_account_without_a_save(self):
        self.add_account("Bob")
        self.fails("/saves/show", 404, "save_not_found", username="Bob")

    def test_backups(self):
        ta.take_backup("Alice", self.path, "base")
        out = self.ok("/saves/backups", username="Alice")
        self.assertEqual(out["backups"], [{"npid": "Alice", "label": "base", "total": 0, "account_rank": 20}])

    def test_log_of_one_account(self):
        self.add_account("Bob", make_save())
        for npid in ("Alice", "Bob", "Alice", "Alice"):
            ta.audit("note", npid)
        self.assertEqual(len(self.ok("/saves/log")["records"]), 4)
        self.assertEqual(len(self.ok("/saves/log", username="Alice", n=2)["records"]), 2)
        self.assertEqual([r["npid"] for r in self.ok("/saves/log", username="bob")["records"]], ["Bob"])

    def test_log_count_must_be_a_number(self):
        self.fails("/saves/log", 400, "invalid_request", n="10")


class SetRankRouteTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.original = bytes(make_save(chars=[(0, 20, 1500, 10, 5)], account_rank=20))
        self.path = self.add_account("Alice", self.original)
        self.sha = srv.sha256(self.original)

    def test_preview_writes_nothing(self):
        out = self.ok("/saves/set-rank", username="Alice", char="paul", rank="genbu", points=3000, dry_run=True)
        self.assertEqual((out["applied"], out["sha256"], out["online"]), (False, self.sha, False))
        self.assertEqual(out["changes"], {"account_rank": None, "chars": [{
            "id": 0, "character": "Paul",
            "before": {"rank": 20, "rank_name": "Berserker", "points": 1500, "streak": 0},
            "after": {"rank": 29, "rank_name": "Genbu", "points": 3000, "streak": 0}}]})
        self.assertEqual((read(self.path), self.backups("Alice")), (self.original, []))

    def test_writing_needs_the_previewed_sha256(self):
        msg = self.fails("/saves/set-rank", 400, "invalid_request", username="Alice", char=0, rank=29)
        self.assertEqual(msg, "expect_sha256 is required")
        self.assertEqual(read(self.path), self.original)

    def test_writes_with_the_previewed_sha256(self):
        out = self.ok("/saves/set-rank", username="Alice", char=0, rank=29, expect_sha256=self.sha)
        self.assertTrue(out["applied"])
        self.assertTrue(out["result"]["landed"])
        self.assertEqual(char_state(read(self.path), 0), (29, 1500, 0))
        self.assertEqual(self.backups("Alice"), [out["result"]["backup"] + ".tdt"])
        (rec,) = self.audit_records()
        self.assertEqual((rec["user"], rec["action"], rec["via"], rec["char"], rec["rank"]),
                         ("Admin", "set-rank", "web", 0, 29))

    def test_refuses_a_save_that_changed_since_the_preview(self):
        self.fails("/saves/set-rank", 409, "save_changed", username="Alice", char=0, rank=29,
                   expect_sha256=srv.sha256(b"older"))
        self.assertEqual(read(self.path), self.original)

    def test_refuses_an_online_account(self):
        self.online = {"Alice"}
        self.fails("/saves/set-rank", 409, "online", username="Alice", char=0, rank=29, expect_sha256=self.sha)
        self.assertEqual(read(self.path), self.original)

    def test_refuses_when_online_status_is_unknown(self):
        self.online = None
        self.fails("/saves/set-rank", 503, "online_unknown", username="Alice", char=0, rank=29,
                   expect_sha256=self.sha)

    def test_nothing_to_change_writes_nothing(self):
        out = self.ok("/saves/set-rank", username="Alice", char=0, rank=20)
        self.assertEqual((out["applied"], out["changes"]["chars"]), (False, []))
        self.assertEqual(self.backups("Alice"), [])

    def test_all_characters(self):
        out = self.ok("/saves/set-rank", username="Alice", char="all", rank=12, expect_sha256=self.sha)
        after = read(self.path)
        self.assertEqual((len(out["changes"]["chars"]), out["changes"]["account_rank"]), (59, {"before": 20, "after": 12}))
        self.assertEqual({char_state(after, i) for i in range(ta.CHAR_N)}, {(12, 5000, 0)})
        self.assertEqual(self.audit_records()[0]["char"], "all")

    def test_bad_arguments(self):
        cases = [dict(char="akuma", rank=1), dict(char=0, rank="platinum"), dict(char=59, rank=1),
                 dict(char=0, rank=1, points="100"), dict(char=0, rank=1, points=True),
                 dict(char="all", rank=12, points=5), dict(rank=1)]
        for args in cases:
            self.fails("/saves/set-rank", 400, "invalid_request", username="Alice", dry_run=True, **args)


class SetAccountRankRouteTest(ServerCase):
    def test_changes_only_the_account_rank(self):
        original = bytes(make_save(chars=[(0, 20, 1500)], account_rank=20))
        path = self.add_account("Alice", original)
        out = self.ok("/saves/set-account-rank", username="Alice", rank="Genbu", expect_sha256=srv.sha256(original))
        self.assertEqual(out["changes"], {"account_rank": {"before": 20, "after": 29}, "chars": []})
        self.assertEqual((read(path)[ta.OFF_ACCOUNT_RANK], char_state(read(path), 0)), (29, (20, 1500, 0)))


class FloorRouteTest(ServerCase):
    def test_preview_then_floor(self):
        original = bytes(make_save(chars=[(0, 30, 100, 5, 5)], account_rank=30))
        path = self.add_account("Alice", original)
        preview = self.ok("/saves/floor", username="Alice", dry_run=True)
        self.assertEqual(preview["floor"], {"reached": 30, "floor": 19, "raised": 58, "points_fixed": 0,
                                            "likely_demoted": False})
        self.assertEqual(len(preview["changes"]["chars"]), 58)
        out = self.ok("/saves/floor", username="Alice", expect_sha256=preview["sha256"])
        self.assertTrue(out["applied"])
        self.assertEqual(char_state(read(path), 1), (19, 5000, 0))
        self.assertEqual(self.audit_records()[0]["floor"], 19)

    def test_demotions_after_an_earlier_floor_need_refloor(self):
        chars = [(0, 30, 100), (1, 17, 0), (2, 17, 0)]
        original = bytes(make_save(every=(19, 5000), chars=chars, account_rank=30))
        path = self.add_account("Alice", original)
        sha = srv.sha256(original)
        self.assertTrue(self.ok("/saves/floor", username="Alice", dry_run=True)["floor"]["likely_demoted"])
        self.fails("/saves/floor", 409, "likely_demoted", username="Alice", expect_sha256=sha)
        self.assertEqual(read(path), original)
        self.ok("/saves/floor", username="Alice", refloor=True, expect_sha256=sha)
        self.assertEqual(char_state(read(path), 1), (19, 5000, 0))

    def test_explicit_rank_must_have_floor_points(self):
        self.add_account("Alice", make_save())
        self.fails("/saves/floor", 400, "invalid_request", username="Alice", rank=0, dry_run=True)
        self.assertEqual(self.ok("/saves/floor", username="Alice", rank="3rd dan", dry_run=True)["floor"]["floor"], 12)


class RestoreRouteTest(ServerCase):
    def setUp(self):
        super().setUp()
        self.original = bytes(make_save(chars=[(0, 20, 1500)], account_rank=20))
        self.path = self.add_account("Alice", self.original)
        ta.take_backup("Alice", self.path, "base")

    def test_restores_a_backup_by_label(self):
        edited = bytearray(self.original)
        ta.set_rank_buf(edited, 0, 29)
        ta.apply_save("Alice", edited, "set-rank")
        current = read(self.path)
        out = self.ok("/saves/restore", username="Alice", label="base", expect_sha256=srv.sha256(current))
        self.assertEqual(out["changes"]["chars"][0]["after"]["rank"], 20)
        self.assertEqual(read(self.path), self.original)
        self.assertEqual(self.audit_records()[-1]["action"], "restore")

    def test_unknown_label(self):
        self.fails("/saves/restore", 404, "backup_not_found", username="Alice", label="never", dry_run=True)

    def test_label_cannot_be_a_path_or_pattern(self):
        for label in ("../Bob/base", "*", ".hidden", "a/b"):
            self.fails("/saves/restore", 400, "invalid_request", username="Alice", label=label, dry_run=True)


class FakeRpcn(BaseHTTPRequestHandler):
    """answers /admin/users/info with the status the test set, /admin/sessions with two sessions"""
    status = 200
    seen = []

    def do_GET(self):
        FakeRpcn.seen.append((self.path, self.headers.get("X-API-Key"), None))
        body = json.dumps({"sessions": [{"online_name": "Shown", "npid": "alice1", "ip": "1.2.3.4"},
                                        {"online_name": "Other", "npid": "bob", "ip": "5.6.7.8"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeRpcn.seen.append((self.path, self.headers.get("X-API-Key"), json.loads(body)))
        self.send_response(self.status)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def start(server):
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class VerifyAdminTest(unittest.TestCase):
    def setUp(self):
        FakeRpcn.seen = []
        rpcn = start(ThreadingHTTPServer(("127.0.0.1", 0), FakeRpcn))
        self.addCleanup(rpcn.server_close)
        self.addCleanup(rpcn.shutdown)
        patches = [mock.patch.object(ta, "RPCN_API_URL", f"http://127.0.0.1:{rpcn.server_port}"),
                   mock.patch.object(ta, "stat_api_key", return_value="rpcn-key")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def answer(self, status):
        FakeRpcn.status = status

    def test_accepts_an_admin(self):
        self.answer(200)
        srv.verify_admin("Admin", "DERIVED")
        self.assertEqual(FakeRpcn.seen, [("/admin/users/info", "rpcn-key",
                                          {"admin_username": "Admin", "admin_password": "DERIVED", "username": "Admin"})])

    def test_maps_rpcn_refusals(self):
        for status, code in ((401, "invalid_credentials"), (403, "forbidden"), (500, "rpcn_unavailable"),
                             (404, "rpcn_unavailable")):
            self.answer(status)
            with self.assertRaises(ta.TdtError) as cm:
                srv.verify_admin("Admin", "DERIVED")
            self.assertEqual(cm.exception.code, code, status)

    def test_online_reads_the_sessions_of_the_configured_rpcn(self):
        self.assertEqual(ta.online(), {"alice1", "bob"})
        self.assertEqual(FakeRpcn.seen, [("/admin/sessions", "rpcn-key", None)])

    def test_unreachable_rpcn(self):
        with mock.patch.object(ta, "RPCN_API_URL", "http://127.0.0.1:9"), self.assertRaises(ta.TdtError) as cm:
            srv.verify_admin("Admin", "DERIVED")
        self.assertEqual(cm.exception.code, "rpcn_unavailable")


class HttpTest(ServerCase):
    """the real socket: status, headers and body as tag2now-BE sees them"""

    def setUp(self):
        super().setUp()
        self.add_account("Alice", make_save(account_rank=20))
        server = start(srv.serve("127.0.0.1:0", KEY))
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}"

    def post(self, path, body, key=KEY):
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"X-API-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.headers["Content-Type"], json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, e.headers["Content-Type"], json.loads(e.read())

    def test_show(self):
        body = json.dumps({"admin_username": "Admin", "admin_password": "x", "username": "Alice"}).encode()
        status, ctype, out = self.post("/saves/show", body)
        self.assertEqual((status, ctype, out["account_rank"]), (200, "application/json; charset=utf-8", 20))

    def test_error_body(self):
        status, _, out = self.post("/saves/show", b"{}", key="wrong")
        self.assertEqual((status, out["error"]), (403, "forbidden"))

    def test_oversized_body(self):
        status, _, out = self.post("/saves/show", b" " * (srv.MAX_BODY + 1))
        self.assertEqual((status, out["error"]), (400, "invalid_request"))

    def test_get_is_not_allowed(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(self.base + "/saves/show", timeout=5)
        self.assertEqual(cm.exception.code, 405)


if __name__ == "__main__":
    unittest.main()
