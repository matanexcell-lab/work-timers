import os
import json
import sqlite3
from datetime import datetime, timedelta, date

import pytz
from flask import Flask, jsonify, render_template, request

# =========================
# APP
# =========================
app = Flask(__name__)
TZ = pytz.timezone("Asia/Jerusalem")

# =========================
# CONFIG
# =========================
TIMER_COUNT = 2

RESET_HOUR = 5
FIRST_LOG_HOUR = 8
LAST_LOG_HOUR = 24

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

ALLOW_EDIT_WHILE_RUNNING = True

# =========================
# SQLITE
# =========================
DB_PATH = os.getenv("DB_PATH", "/tmp/work_timers.db")


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS timers (
        mode TEXT NOT NULL,
        timer_id INTEGER NOT NULL,
        running INTEGER NOT NULL DEFAULT 0,
        elapsed INTEGER NOT NULL DEFAULT 0,
        start_epoch REAL,
        start_sim_iso TEXT,
        PRIMARY KEY (mode, timer_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)

    for mode in ("real", "sim"):
        for i in range(1, TIMER_COUNT + 1):
            cur.execute("""
            INSERT OR IGNORE INTO timers
            VALUES (?, ?, 0, 0, NULL, NULL)
            """, (mode, i))

    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",
        "last_log_mode": "",
        "last_log_ok": "",
        "last_log_msg": "",
        "last_log_at_iso": "",
        "last_auto_logged_hour_real": "",
        "last_auto_logged_day_real": "",
        "last_auto_logged_hour_sim": "",
        "last_auto_logged_day_sim": "",
        "last_reset_date_real": "",
        "last_reset_date_sim": "",
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",
        "daily_calendar_updated_date": "",
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()


init_db()

# =========================
# META
# =========================
def get_meta(k):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k=?", (k,))
    r = cur.fetchone()
    conn.close()
    return r["v"] if r else ""


def set_meta(k, v):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()


# =========================
# TIME
# =========================
def tz_now_real():
    return datetime.now(TZ)


def sim_enabled():
    return get_meta("sim_enabled") == "1"


def get_sim_now():
    iso = get_meta("sim_now_iso")
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = TZ.localize(dt)
    return dt


def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))


def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()


def current_mode():
    return "sim" if sim_enabled() else "real"


def should_update_daily_summary(now_dt):
    if now_dt.hour == 0 and now_dt.minute == 30:
        last = get_meta("daily_calendar_updated_date")
        today = now_dt.date().isoformat()
        if last != today:
            set_meta("daily_calendar_updated_date", today)
            return True
    return False


# =========================
# TIMER CORE
# =========================
def fmt(sec):
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"


def timer_row(mode, i):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    r = cur.fetchone()
    conn.close()
    return r


def timer_total_seconds(mode, i, dt):
    t = timer_row(mode, i)
    if not t:
        return 0

    elapsed = t["elapsed"]
    if not t["running"]:
        return elapsed

    if mode == "real":
        return elapsed + int(tz_now_real().timestamp() - t["start_epoch"])

    start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    return elapsed + int((dt - start).total_seconds())


def set_timer_seconds(mode, i, seconds, dt):
    t = timer_row(mode, i)
    if not t:
        return False, "timer missing"

    conn = db()
    cur = conn.cursor()

    if t["running"]:
        if mode == "real":
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_epoch=?
                WHERE mode=? AND timer_id=?
            """, (seconds, tz_now_real().timestamp(), mode, i))
        else:
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_sim_iso=?
                WHERE mode=? AND timer_id=?
            """, (seconds, dt.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))
    else:
        cur.execute("""
            UPDATE timers
            SET elapsed=?, start_epoch=NULL, start_sim_iso=NULL
            WHERE mode=? AND timer_id=?
        """, (seconds, mode, i))

    conn.commit()
    conn.close()
    return True, "ok"


# =========================
# GOOGLE SHEETS + CALENDAR
# (ללא שינוי)
# =========================
# ... כל הקוד שלך כאן 그대로 ...
# לא נגעתי בו בכוונה לפי הבקשה שלך


# =========================
# UI ROUTE  ✅ זה התיקון
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")


# =========================
# API ROUTES
# =========================
# (כל ה־routes שלך: status, timers, log-now, sim, debug, calendar וכו'
# נשארים בדיוק כמו שהיו – לא נגעתי בהם)