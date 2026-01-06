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
    return TZ.localize(dt)


def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))


def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()


def current_mode():
    return "sim" if sim_enabled() else "real"


# =========================
# TIMERS
# =========================
def fmt(sec):
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

    start = datetime.fromisoformat(t["start_sim_iso"])
    start = TZ.localize(start)
    return elapsed + int((dt - start).total_seconds())


# =========================
# GOOGLE SHEETS
# =========================
WS = None


def gs_connect():
    global WS
    if WS:
        return WS

    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    gc = gspread.authorize(creds)
    WS = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS


def log_start_time_if_needed(mode, clock_dt):
    if clock_dt is None or clock_dt.hour < RESET_HOUR:
        return

    ws = gs_connect()
    if ws is None:
        return

    day = clock_dt.date()
    key = f"first_start_logged_date_{mode}"
    if get_meta(key) == day.isoformat():
        return

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    ws.update_cell(4, col, clock_dt.strftime("%H:%M"))
    set_meta(key, day.isoformat())


# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")


@app.route("/api/status")
def status():
    mode = current_mode()
    clock = now_for_mode(mode)

    timers = [
        fmt(timer_total_seconds(mode, i, clock))
        for i in range(1, TIMER_COUNT + 1)
    ]

    return jsonify({
        "now_str": clock.strftime("%d/%m/%Y %H:%M:%S") if clock else "",
        "simulation": sim_enabled(),
        "mode": mode,
        "timers": timers,
        "last_log": {
            "mode": get_meta("last_log_mode"),
            "ok": get_meta("last_log_ok"),
            "msg": get_meta("last_log_msg"),
            "at": get_meta("last_log_at_iso"),
        }
    })


@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    mode = current_mode()
    clock = now_for_mode(mode)

    # ✅ התיקון היחיד – Start לא ייכשל בגלל Google
    try:
        log_start_time_if_needed(mode, clock)
    except Exception as e:
        print("⚠️ start time log skipped:", e)

    conn = db()
    cur = conn.cursor()

    if mode == "real":
        cur.execute("""
            UPDATE timers
            SET running=1, start_epoch=?, start_sim_iso=NULL
            WHERE mode=? AND timer_id=?
        """, (tz_now_real().timestamp(), mode, i))
    else:
        cur.execute("""
            UPDATE timers
            SET running=1, start_sim_iso=?, start_epoch=NULL
            WHERE mode=? AND timer_id=?
        """, (clock.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))

    conn.commit()
    conn.close()
    return ("", 204)


@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    mode = current_mode()
    clock = now_for_mode(mode)
    total = timer_total_seconds(mode, i, clock)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE timers
        SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL
        WHERE mode=? AND timer_id=?
    """, (total, mode, i))
    conn.commit()
    conn.close()
    return ("", 204)


@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    mode = current_mode()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE timers
        SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
        WHERE mode=? AND timer_id=?
    """, (mode, i))
    conn.commit()
    conn.close()
    return ("", 204)


if __name__ == "__main__":
    app.run(debug=True)