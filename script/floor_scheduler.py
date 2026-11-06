"""The two floors on a schedule, run inside tdt_admin_server.

Every minute, floor_new_users seeds and floors new accounts. Once a day, from
DAILY_AT UTC, `floor --all` raises every save to its floor. The time it last
ran is kept beside the audit log, so a restart catches up on a missed day
instead of skipping it. FLOOR_SCHEDULER=off turns both off.
"""
import os
import json
import time
import logging
import threading
import datetime as dt

import tdt_admin as ta
import floor_new_users as fn

TICK = 60
DAILY_AT = dt.time(6, 10)
LAST_DAILY = os.path.join(ta.BACKUP_DIR, "floor_daily.json")

log = logging.getLogger("floor-scheduler")


def daily_due(now, last_run):
    """whether floor --all is due at now, having last run at last_run (None: never)"""
    slot = dt.datetime.combine(now.date(), DAILY_AT, tzinfo=dt.timezone.utc)
    if now < slot:
        slot -= dt.timedelta(days=1)
    return last_run is None or last_run < slot


def last_daily():
    try:
        with open(LAST_DAILY, encoding="utf-8") as f:
            return dt.datetime.fromisoformat(json.load(f)["last_run"])
    except FileNotFoundError:
        return None


def record_daily(now):
    os.makedirs(os.path.dirname(LAST_DAILY), exist_ok=True)
    tmp = LAST_DAILY + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"last_run": now.isoformat(timespec="seconds")}, f)
    os.replace(tmp, LAST_DAILY)


def attempt(name, job):
    """job(), logged; True if it finished, so one failure never stops the schedule"""
    try:
        job()
        return True
    except ta.TdtError as e:
        log.error("%s failed: %s", name, e)
        return False
    except Exception:
        log.exception("%s failed", name)
        return False


def tick(now):
    """one pass: new users every time, then the daily floor when it is due"""
    attempt("new users floor", fn.run)
    if daily_due(now, last_daily()) and attempt("daily floor", lambda: ta.floor_accounts(ta.save_owners())):
        record_daily(now)


def start():
    """the scheduler thread, or None when FLOOR_SCHEDULER=off"""
    if os.environ.get("FLOOR_SCHEDULER") == "off":
        log.info("floor scheduler off")
        return None
    thread = threading.Thread(target=_loop, name="floor-scheduler", daemon=True)
    thread.start()
    log.info("floor scheduler on: new users every %ds, all saves daily from %s UTC", TICK, DAILY_AT)
    return thread


def _loop():
    while True:
        tick(dt.datetime.now(dt.timezone.utc))
        time.sleep(TICK)
