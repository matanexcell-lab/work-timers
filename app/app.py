import os
import json
import sqlite3
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, render_template, request

# =========================
# APP
# =========================
app = Flask(__name__, template_folder="templates")
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
        mode TEXT,
        timer_id INTEGER,
        running INTEGER,
        elapsed INTEGER,
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
        "last_logged_hour": "",
        "last_logged_day": "",
        "last_reset_date": "",
        "first_start_logged_date": "",
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

init_db()

# =========================
# HELPERS
# =========================
def tz_now_real():
    return datetime.now(TZ)

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

def now():
    return get_sim_now() if sim_enabled() else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

# =========================
# TIMER LOGIC
# =========================
def timer_total_seconds(mode, timer_id, clock):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, timer_id))
    t = cur.fetchone()
    conn.close()

    elapsed = int(t["elapsed"])
    if not t["running"]:
        return elapsed

    if mode == "real":
        return elapsed + int(tz_now_real().timestamp() - t["start_epoch"])

    start = datetime.fromisoformat(t["start_sim_iso"])
    start = TZ.localize(start)
    return elapsed + int((clock - start).total_seconds())

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

    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
    )

    gc = gspread.authorize(creds)
    WS = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def target_hour_and_date(dt):
    if dt.hour < FIRST_LOG_HOUR:
        return 23, dt.date() - timedelta(days=1)
    if dt.hour > 23:
        return 23, dt.date()
    return dt.hour, dt.date()

# 🔧🔧🔧 התיקון היחיד כאן 🔧🔧🔧
def write_two_timers(ws, row, base_col, mode, clock):
    t1 = fmt(timer_total_seconds(mode, 1, clock))
    t2 = fmt(timer_total_seconds(mode, 2, clock))

    ws.update_cell(row, base_col, t1)
    ws.update_cell(row, base_col + 1, t2)

def log_to_sheet(force=False):
    ws = gs_connect()
    if not ws:
        return False, "no creds"

    clock = tz_now_real()
    hour, day = target_hour_and_date(clock)

    if not force:
        if get_meta("last_logged_hour") == str(hour) and get_meta("last_logged_day") == day.isoformat():
            return True, "skip"

    headers = ws.row_values(3)
    date_str = day.strftime("%d/%m/%Y")
    if date_str not in headers:
        return False, "date missing"

    col = headers.index(date_str) + 1
    row = min(22, max(7, 7 + (hour - 8)))

    write_two_timers(ws, row, col, "real", clock)

    set_meta("last_logged_hour", str(hour))
    set_meta("last_logged_day", day.isoformat())
    return True, "ok"

# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    clock = now()

    if sim_enabled():
        set_sim_now(clock + timedelta(seconds=1))
        clock = get_sim_now()

    if tz_now_real().minute == 0 and tz_now_real().second == 0:
        log_to_sheet()

    timers = [fmt(timer_total_seconds(current_mode(), i, clock)) for i in range(1, 3)]

    return jsonify({
        "now_str": clock.strftime("%d/%m/%Y %H:%M:%S"),
        "timers": timers,
        "simulation": sim_enabled()
    })

@app.route("/api/log-now", methods=["POST"])
def manual_log():
    ok, msg = log_to_sheet(force=True)
    return jsonify(ok=ok, message=msg)

if __name__ == "__main__":
    app.run(debug=True)