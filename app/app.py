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
        "last_reset_date": "",
        "last_logged_hour": "",
        "last_logged_day": "",
        "first_start_logged_date": ""
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

init_db()

# =========================
# TIME HELPERS
# =========================
def tz_now_real():
    return datetime.now(TZ)

def get_meta(k):
    conn = db()
    v = conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    conn.close()
    return v["v"] if v else ""

def set_meta(k, v):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

def sim_enabled():
    return get_meta("sim_enabled") == "1"

def get_sim_now():
    iso = get_meta("sim_now_iso")
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    return TZ.localize(dt)

def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat())

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
    conn.execute("""
    UPDATE timers
    SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode='real'
    """)
    conn.commit()
    conn.close()

    set_meta("last_reset_date", today)
    set_meta("first_start_logged_date", "")

# =========================
# TIMER CALC
# =========================
def timer_total_seconds(mode, i, n):
    conn = db()
    t = conn.execute(
        "SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i)
    ).fetchone()
    conn.close()

    total = t["elapsed"]
    if not t["running"]:
        return total

    if mode == "real":
        return total + int(tz_now_real().timestamp() - t["start_epoch"])
    else:
        start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
        return total + int((n - start).total_seconds())

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

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

def log_to_sheet(force=False):
    ws = gs_connect()
    if not ws:
        return False

    n = tz_now_real()
    hour, day = target_hour_and_date(n)

    if (
        not force
        and get_meta("last_logged_hour") == str(hour)
        and get_meta("last_logged_day") == day.isoformat()
    ):
        return True

    headers = ws.row_values(3)
    date_str = day.strftime("%d/%m/%Y")
    if date_str not in headers:
        return False

    col = headers.index(date_str) + 1
    row = max(7, min(22, 7 + hour - 8))

    total = sum(
        timer_total_seconds("real", i, tz_now_real())
        for i in range(1, TIMER_COUNT + 1)
    )

    ws.update_cell(row, col, fmt(total))
    set_meta("last_logged_hour", str(hour))
    set_meta("last_logged_day", day.isoformat())
    return True

# =========================
# ⭐ FIXED AUTO LOG (REAL + SIM)
# =========================
def maybe_auto_log():
    real = tz_now_real()
    sim = get_sim_now() if sim_enabled() else None

    if real.minute == 0 and real.second == 0:
        if FIRST_LOG_HOUR <= real.hour <= LAST_LOG_HOUR:
            log_to_sheet()

    if sim and sim.minute == 0 and sim.second == 0:
        if FIRST_LOG_HOUR <= sim.hour <= LAST_LOG_HOUR:
            log_to_sheet()

# =========================
# ROUTES
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    n = now()
    ensure_daily_reset(tz_now_real())
    maybe_auto_log()

    if sim_enabled():
        set_sim_now(n + timedelta(seconds=1))
        n = get_sim_now()

    mode = current_mode()
    timers = [
        fmt(timer_total_seconds(mode, i, n))
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
    conn = db()
    conn.execute(
        "UPDATE timers SET running=1, start_epoch=?, start_sim_iso=? WHERE mode=? AND timer_id=?",
        (
            tz_now_real().timestamp(),
            n.replace(tzinfo=None).isoformat(),
            current_mode(),
            i,
        ),
    )
    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    n = now()
    total = timer_total_seconds(current_mode(), i, n)
    conn = db()
    conn.execute(
        "UPDATE timers SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL WHERE mode=? AND timer_id=?",
        (total, current_mode(), i),
    )
    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/log-now", methods=["POST"])
def manual_log():
    ok = log_to_sheet(force=True)
    return jsonify(ok=ok)

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