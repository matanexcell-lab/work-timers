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
            (mode, timer_id, running, elapsed)
            VALUES (?, ?, 0, 0)
            """, (mode, i))

    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",
        "last_reset_date": "",
        "last_logged_hour": "",
        "last_logged_day": "",
        "first_start_logged_date": "",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta(k, v) VALUES (?, ?)", (k, v))

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
    row = cur.fetchone()
    conn.close()
    return row["v"] if row else ""

def set_meta(k, v):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, v))
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
    return TZ.localize(dt) if dt.tzinfo is None else dt

def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now():
    return get_sim_now() if sim_enabled() else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# RESET 05:00
# =========================
def ensure_daily_reset(n):
    if n.hour < RESET_HOUR:
        return

    today = n.date().isoformat()
    if get_meta("last_reset_date") == today:
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers
    SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode='real'
    """)
    conn.commit()
    conn.close()

    set_meta("last_reset_date", today)
    set_meta("first_start_logged_date", "")

# =========================
# TIMER
# =========================
def timer_total_seconds(mode, i, n):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    conn.close()

    if not t:
        return 0

    total = int(t["elapsed"])
    if int(t["running"]) == 0:
        return total

    if mode == "real":
        return total + int(tz_now_real().timestamp() - t["start_epoch"])

    start = datetime.fromisoformat(t["start_sim_iso"])
    start = TZ.localize(start)
    return total + int((n - start).total_seconds())

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

# =========================
# GOOGLE SHEET
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
        ],
    )
    gc = gspread.authorize(creds)
    WS = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def target_hour_and_date(n):
    if n.hour < FIRST_LOG_HOUR:
        return 23, n.date() - timedelta(days=1)
    if n.hour > 23:
        return 23, n.date()
    return n.hour, n.date()

def log_start_time_if_needed(clock_dt):
    if clock_dt.hour < RESET_HOUR:
        return

    ws = gs_connect()
    if not ws:
        return

    _, day = target_hour_and_date(clock_dt)
    day_key = day.isoformat()

    if get_meta("first_start_logged_date") == day_key:
        return

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    start = clock_dt.strftime("%H:%M")

    # ✅ התיקון: כתיבה לשני הטיימרים
    ws.update_cell(4, col, start)
    ws.update_cell(4, col + 1, start)

    set_meta("first_start_logged_date", day_key)

# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    n = now()
    ensure_daily_reset(tz_now_real())

    if sim_enabled():
        set_sim_now(n + timedelta(seconds=1))
        n = get_sim_now()

    timers = [
        fmt(timer_total_seconds(current_mode(), i, n))
        for i in range(1, TIMER_COUNT + 1)
    ]

    return jsonify(
        now_str=n.strftime("%d/%m/%Y %H:%M:%S"),
        simulation=sim_enabled(),
        timers=timers,
    )

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    n = now()
    log_start_time_if_needed(n)

    conn = db()
    cur = conn.cursor()
    mode = current_mode()

    if mode == "real":
        cur.execute("""
        UPDATE timers SET running=1, start_epoch=?
        WHERE mode=? AND timer_id=? AND running=0
        """, (tz_now_real().timestamp(), mode, i))
    else:
        cur.execute("""
        UPDATE timers SET running=1, start_sim_iso=?
        WHERE mode=? AND timer_id=? AND running=0
        """, (n.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))

    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    mode = current_mode()
    n = now()
    total = timer_total_seconds(mode, i, n)

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
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers
    SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE timer_id=?
    """, (i,))
    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    dt = datetime.strptime(request.json["datetime"], "%Y-%m-%d %H:%M")
    set_meta("sim_enabled", "1")
    set_sim_now(TZ.localize(dt))
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    set_meta("sim_enabled", "0")
    set_meta("sim_now_iso", "")
    return jsonify(ok=True)

if __name__ == "__main__":
    app.run(debug=True)