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
            INSERT OR IGNORE INTO timers(mode, timer_id)
            VALUES (?, ?)
            """, (mode, i))

    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",
        "last_reset_date": "",
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",
        "last_logged_hour_real": "",
        "last_logged_day_real": "",
        "last_logged_hour_sim": "",
        "last_logged_day_sim": "",
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta(k, v) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

init_db()

# =========================
# META HELPERS
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
    cur.execute("INSERT OR REPLACE INTO meta(k,v) VALUES (?,?)", (k, v))
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
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat())

def now():
    return get_sim_now() if sim_enabled() else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# RESET 05:00
# =========================
def ensure_daily_reset(real_now):
    if real_now.hour < RESET_HOUR:
        return
    today = real_now.date().isoformat()
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
    set_meta("first_start_logged_date_real", "")
    set_meta("first_start_logged_date_sim", "")

# =========================
# TIMER
# =========================
def timer_total_seconds(mode, i, clock):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    conn.close()

    total = int(t["elapsed"])
    if not t["running"]:
        return total

    if mode == "real":
        return total + int(tz_now_real().timestamp() - t["start_epoch"])
    else:
        start = datetime.fromisoformat(t["start_sim_iso"])
        start = TZ.localize(start) if start.tzinfo is None else start
        return total + int((clock - start).total_seconds())

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

# =========================
# GOOGLE SHEETS
# =========================
WS = None

def gs():
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
            "https://www.googleapis.com/auth/drive",
        ],
    )
    WS = gspread.authorize(creds).open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def target_hour_and_date(dt):
    if dt.hour < FIRST_LOG_HOUR:
        return 23, dt.date() - timedelta(days=1)
    if dt.hour > 23:
        return 23, dt.date()
    return dt.hour, dt.date()

def log_to_sheet(mode, clock, force=False):
    ws = gs()
    if not ws:
        return

    hour, day = target_hour_and_date(clock)
    day_key = day.isoformat()

    last_h = get_meta(f"last_logged_hour_{mode}")
    last_d = get_meta(f"last_logged_day_{mode}")

    if not force and last_h == str(hour) and last_d == day_key:
        return

    headers = ws.row_values(3)
    date_str = day.strftime("%d/%m/%Y")
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    row = min(22, max(7, 7 + (hour - 8)))

    total = sum(timer_total_seconds(mode, i, clock) for i in range(1, TIMER_COUNT + 1))
    ws.update_cell(row, col, fmt(total))

    set_meta(f"last_logged_hour_{mode}", str(hour))
    set_meta(f"last_logged_day_{mode}", day_key)

# =========================
# 🔧 FIX – AUTO LOG (THE ONLY CHANGE)
# =========================
def maybe_auto_log_for_mode(mode, clock):
    if not clock:
        return

    hour = clock.hour
    if not (FIRST_LOG_HOUR <= hour <= LAST_LOG_HOUR):
        return

    _, day = target_hour_and_date(clock)
    day_key = day.isoformat()

    last_h = get_meta(f"last_logged_hour_{mode}")
    last_d = get_meta(f"last_logged_day_{mode}")

    if last_h != str(hour) or last_d != day_key:
        log_to_sheet(mode, clock, force=False)

# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    real_now = tz_now_real()
    ensure_daily_reset(real_now)

    clock = now()
    if sim_enabled():
        set_sim_now(clock + timedelta(seconds=1))
        clock = get_sim_now()

    maybe_auto_log_for_mode("real", real_now)
    maybe_auto_log_for_mode("sim", clock)

    mode = current_mode()
    timers = [fmt(timer_total_seconds(mode, i, clock)) for i in range(1, TIMER_COUNT + 1)]

    return jsonify(
        now_str=clock.strftime("%d/%m/%Y %H:%M:%S"),
        simulation=sim_enabled(),
        timers=timers,
    )

# =========================
# START / STOP / RESET
# =========================
@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    mode = current_mode()
    clock = now()
    conn = db()
    cur = conn.cursor()

    if mode == "real":
        cur.execute("""
        UPDATE timers SET running=1, start_epoch=?
        WHERE mode='real' AND timer_id=?
        """, (tz_now_real().timestamp(), i))
    else:
        cur.execute("""
        UPDATE timers SET running=1, start_sim_iso=?
        WHERE mode='sim' AND timer_id=?
        """, (clock.replace(tzinfo=None).isoformat(), i))

    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    mode = current_mode()
    clock = now()
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

# =========================
# MANUAL LOG
# =========================
@app.route("/api/log-now", methods=["POST"])
def manual_log():
    log_to_sheet("real", tz_now_real(), force=True)
    if sim_enabled():
        log_to_sheet("sim", now(), force=True)
    return jsonify(ok=True)

# =========================
# SIMULATION
# =========================
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

# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run(debug=True)