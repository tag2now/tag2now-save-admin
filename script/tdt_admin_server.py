#!/usr/bin/env python3
"""HTTP front for tdt_admin, for the tag2now admin page.

Serves a few of tdt_admin's commands as JSON, from a container on the RPCN host
(see Dockerfile). tag2now-BE is the only caller.

Every request carries X-API-Key: $SAVE_ADMIN_KEY. The /saves routes forward
an admin's request, and their body also carries the admin's RPCN username and
password (as RPCS3 derives it). The password is checked with RPCN's own admin
API on each request and never stored, so a revoked or banned admin loses access
at once.

    GET  /player/save?username=NPID

/player/save is the read-only view tag2now-BE shows on any player's profile, so
it needs only the key and answers only ranks and records: nothing that locates
the file, fingerprints it, or says whether the player is online. It is the one
GET: the /saves routes are POST so the admin password never lands in a URL.

    POST /saves/show                {username, all_chars?}
    POST /saves/backups             {username}
    POST /saves/log                 {username?, n?}
    POST /saves/set-rank            {username, char, rank, points?}        + write fields
    POST /saves/set-account-rank    {username, rank}                       + write fields
    POST /saves/floor               {username, rank?, fix_points?, refloor?} + write fields
    POST /saves/restore             {username, label}                      + write fields

Write fields: dry_run (preview only) and expect_sha256 (required to write: the
sha256 a preview returned, so an edit never lands on a save that changed since).
Errors are {"error": "<code>", "message": "..."}.

    SAVE_ADMIN_KEY=... [RPCN_STAT_API_KEY=...] [RPCN_API_URL=...] tdt_admin_server.py [--bind HOST:PORT]
"""
import os
import re
import sys
import json
import hmac
import hashlib
import logging
import argparse
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import tdt_admin as ta

DEFAULT_BIND = "127.0.0.1:31316"
MAX_BODY = 64 * 1024
MAX_LOG = 500

STATUS = {
    "invalid_request": 400, "ambiguous_user": 400,
    "invalid_credentials": 401, "invalid_api_key": 403, "forbidden": 403,
    "not_found": 404, "user_not_found": 404, "save_not_found": 404, "backup_not_found": 404,
    "method_not_allowed": 405,
    "online": 409, "save_changed": 409, "likely_demoted": 409,
    "rpcn_unavailable": 502, "online_unknown": 503,
}

log = logging.getLogger("tdt-admin-server")


# ---------------------------------------------------------------- request
def need(req, name):
    value = req.get(name)
    if value is None or value == "":
        ta.die(f"{name} is required")
    return value


def integer(req, name, default=None):
    value = req.get(name, default)
    # bool is an int in Python; true is not a number of points
    if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
        ta.die(f"{name} must be an integer")
    return value


def parsed(parse, value):
    """a rank or character, by number or name, through tdt_admin's own parsers"""
    try:
        return parse(str(value))
    except argparse.ArgumentTypeError as e:
        ta.die(str(e))


def account(req, name="username"):
    return ta.resolve(need(req, name))


def safe_label(label):
    # a backup label is a file name; no paths and no glob patterns
    if not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*", label):
        ta.die(f"bad backup label {label!r}")
    return label


# ------------------------------------------------------------------- admin
def verify_admin(username, password):
    """RPCN's admin API says whether this is an admin that is not banned; TdtError if not"""
    body = json.dumps({"admin_username": username, "admin_password": password,
                       "username": username}).encode()
    req = urllib.request.Request(ta.RPCN_API_URL + "/admin/users/info", data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-API-Key": ta.stat_api_key()})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return
    except urllib.error.HTTPError as e:
        status = e.code
    except OSError as e:
        log.error("RPCN API unreachable at %s: %s", ta.RPCN_API_URL, e)
        ta.die("RPCN API server unreachable", "rpcn_unavailable")
    if status == 401:
        ta.die("wrong admin username or password", "invalid_credentials")
    if status == 403:
        # RPCN answers 403 to a non-admin and to a wrong RPCN_STAT_API_KEY alike
        log.warning("RPCN refused %s: not an active admin, or a wrong RPCN_STAT_API_KEY", username)
        ta.die("not an admin", "forbidden")
    log.error("RPCN API answered %s for the admin check", status)
    ta.die("RPCN API server failed", "rpcn_unavailable")


# ------------------------------------------------------------------- saves
def online_state(npid):
    """True or False, None when the API server cannot tell"""
    who = ta.online()
    return None if who is None else npid in who


def live(npid):
    return ta.read_save(ta.lookup(npid)[3])


def sha256(b):
    return hashlib.sha256(bytes(b)).hexdigest()


def _slot(c):
    return {"rank": c["rank"], "rank_name": c["rank_name"], "points": c["points"], "streak": c["streak"]}


def diff_saves(before, after):
    """{account_rank: {before, after} or None, chars: [{id, character, before, after}]}
    for what differs; records are never edited, so only rank, points and streak count"""
    a, b = ta.decode(before, all_chars=True), ta.decode(after, all_chars=True)
    chars = [{"id": x["id"], "character": x["character"], "before": _slot(x), "after": _slot(y)}
             for x, y in zip(a["chars"], b["chars"]) if _slot(x) != _slot(y)]
    acc = None
    if a["account_rank"] != b["account_rank"]:
        acc = {"before": a["account_rank"], "after": b["account_rank"]}
    return {"account_rank": acc, "chars": chars}


def write(req, admin, npid, before, after, action, **meta):
    """the preview of before -> after, then the write unless dry_run or nothing changes"""
    changes = diff_saves(before, after)
    out = {"username": npid, "sha256": sha256(before), "online": online_state(npid),
           "changes": changes, "applied": False, "result": None}
    if req.get("dry_run") or not (changes["chars"] or changes["account_rank"]):
        return out
    expect = need(req, "expect_sha256")
    r = ta.apply_save(npid, after, action, actor=admin, expect_sha256=expect, via="web", **meta)
    out["applied"] = True
    out["result"] = {"backup": os.path.basename(r["backup"])[:-4], "checksum": f"0x{r['checksum']:08X}",
                     "data_id": r["data_id"], "landed": r["landed"]}
    log.info("%s %s %s: %d characters, landed %s", admin, action, npid, len(changes["chars"]), r["landed"])
    return out


# ------------------------------------------------------------------ routes
PLAYER_SAVE_FIELDS = ("npid", "saved_utc", "account_rank", "total", "wins", "losses", "chars")


def route_player_save(req):
    d = ta.show_save(account(req))
    return {k: d[k] for k in PLAYER_SAVE_FIELDS}


def route_show(req, admin):
    npid = account(req)
    return {**ta.show_save(npid, bool(req.get("all_chars"))), "online": online_state(npid)}


def route_backups(req, admin):
    return {"backups": ta.list_backups(account(req))}


def route_log(req, admin):
    n = min(max(integer(req, "n", 50), 1), MAX_LOG)
    npid = account(req) if req.get("username") else None
    return {"records": ta.read_log(n, npid)}


def route_set_rank(req, admin):
    npid = account(req)
    char = parsed(ta.parse_char, need(req, "char"))
    rank = parsed(ta.parse_rank, need(req, "rank"))
    points = integer(req, "points")
    ta.check_rank_edit(char, rank, points)
    before = live(npid)
    after = bytearray(before)
    if char == ta.ALL_CHARS:
        ta.set_all_buf(after, rank)
    else:
        ta.set_rank_buf(after, char, rank, points)
    return write(req, admin, npid, before, after, "set-rank",
                 char="all" if char == ta.ALL_CHARS else char, rank=rank, points=points)


def route_set_account_rank(req, admin):
    npid = account(req)
    rank = parsed(ta.parse_rank, need(req, "rank"))
    if not 0 <= rank <= 255:
        ta.die("rank must be 0..255")
    before = live(npid)
    after = bytearray(before)
    after[ta.OFF_ACCOUNT_RANK] = rank
    return write(req, admin, npid, before, after, "set-account-rank", rank=rank)


def route_floor(req, admin):
    npid = account(req)
    rank = None if req.get("rank") in (None, "") else parsed(ta.parse_rank, req["rank"])
    if rank is not None:
        ta.check_floor_rank(rank)
    fix_points = bool(req.get("fix_points"))
    before = live(npid)
    after = bytearray(before)
    m, y, n = ta.floor_buf(after, rank)
    f = ta.fix_floor_points(after, y) if fix_points else 0
    demoted = ta.likely_demotions(n) and not req.get("refloor")
    if demoted and not req.get("dry_run"):
        ta.die(f"the {n} characters below the floor look like demotions after an earlier floor; "
               f"pass refloor to raise them anyway", "likely_demoted")
    out = write(req, admin, npid, before, after, "floor", floor=y, raised=n,
                **({"points_fixed": f} if fix_points else {}))
    out["floor"] = {"reached": m, "floor": y, "raised": n, "points_fixed": f, "likely_demoted": demoted}
    return out


def route_restore(req, admin):
    npid = account(req)
    src = ta.find_backup(npid, safe_label(need(req, "label")))
    return write(req, admin, npid, live(npid), ta.read_save(src), "restore", source=src)


def as_admin(route):
    """route behind RPCN's admin check; it is given the checked admin's username"""
    def checked(req):
        admin = need(req, "admin_username")
        verify_admin(admin, need(req, "admin_password"))
        return route(req, admin)
    return checked


ROUTES = {
    ("GET", "/player/save"): route_player_save,
    ("POST", "/saves/show"): as_admin(route_show),
    ("POST", "/saves/backups"): as_admin(route_backups),
    ("POST", "/saves/log"): as_admin(route_log),
    ("POST", "/saves/set-rank"): as_admin(route_set_rank),
    ("POST", "/saves/set-account-rank"): as_admin(route_set_account_rank),
    ("POST", "/saves/floor"): as_admin(route_floor),
    ("POST", "/saves/restore"): as_admin(route_restore),
}


# -------------------------------------------------------------------- http
def error(code, message):
    return STATUS.get(code, 500), {"error": code, "message": message}


def unrouted(method, path):
    if any(known == path for _, known in ROUTES):
        return error("method_not_allowed", f"{path} does not take {method}")
    return error("not_found", f"no route {path}")


def query_args(query):
    """a GET's query string as a request: one value per name"""
    return {name: values[-1] for name, values in urllib.parse.parse_qs(query).items()}


def request_body(raw):
    try:
        req = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        ta.die("body is not JSON")
    if not isinstance(req, dict):
        ta.die("body must be a JSON object")
    return req


def handle(api_key, method, target, given_key, raw=b""):
    """one request -> (status, body); the HTTP layer only moves bytes.
    A GET's arguments are its query string, a POST's its JSON body."""
    if not hmac.compare_digest((given_key or "").encode(), api_key.encode()):
        return error("invalid_api_key", "missing or wrong X-API-Key")
    url = urllib.parse.urlsplit(target)
    route = ROUTES.get((method, url.path))
    if route is None:
        return unrouted(method, url.path)
    try:
        req = query_args(url.query) if method == "GET" else request_body(raw)
        return 200, route(req)
    except ta.TdtError as e:
        return error(e.code, str(e))
    except Exception:
        log.exception("%s %s failed", method, url.path)
        return error("internal_error", "internal error; see the server log")


class Handler(BaseHTTPRequestHandler):
    server_version = "tdt-admin-server"
    api_key = ""

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 <= size <= MAX_BODY:
            self.send_json(*error("invalid_request", f"body over {MAX_BODY} bytes"))
            return
        raw = self.rfile.read(size)
        self.send_json(*handle(self.api_key, "POST", self.path, self.headers.get("X-API-Key"), raw))

    def do_GET(self):
        self.send_json(*handle(self.api_key, "GET", self.path, self.headers.get("X-API-Key")))

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def serve(bind, api_key):
    """a server for HOST:PORT; port 0 picks a free one"""
    host, _, port = bind.rpartition(":")
    handler = type("BoundHandler", (Handler,), {"api_key": api_key})
    return ThreadingHTTPServer((host, int(port)), handler)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bind", default=os.environ.get("TDT_ADMIN_BIND", DEFAULT_BIND),
                   help=f"HOST:PORT to listen on (env TDT_ADMIN_BIND, default {DEFAULT_BIND})")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.environ.get("SAVE_ADMIN_KEY", "")
    if not api_key:
        sys.exit("error: SAVE_ADMIN_KEY is not set; refusing to serve without a key")
    server = serve(a.bind, api_key)
    log.info("listening on %s, RPCN API at %s", a.bind, ta.RPCN_API_URL)
    server.serve_forever()


if __name__ == "__main__":
    main()
