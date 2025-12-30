import os
import json
import sqlite3
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, render_template, request

app = Flask(__name__, template_folder="templates")
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
RESET_HOUR = 5
FIRST_LOG_HOUR = 8
LAST_LOG_HOUR = 24

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

DB_PATH = os.getenv("DB_PATH", "/tmp/work_timers.db")

# =========================
# DB
# =========================
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
            INSERT OR IGNORE INTO timers VALUES (?, ?, 0, 0, NULL, NULL)
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
# META
# =========================
def get_meta(k):
    c = db(); cur = c.cursor()
    cur.execute("SELECT v FROM meta WHERE k=?", (k,))
    r = cur.fetchone()
    c.close()
    return r["v"] if r else ""

def set_meta(k, v):
    c = db(); cur = c.cursor()
    cur.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))
    c.commit(); c.close()

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
    return TZ.localize(datetime.fromisoformat(iso))

def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now():
    return get_sim_now() if sim_enabled() else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# RESET 05:00
# =========================
def ensure_daily_reset():
    n = tz_now_real()
    if n.hour < RESET_HOUR:
        return

    today = n.date().isoformat()
    if get_meta("last_reset_date") == today:
        return

    c = db(); cur = c.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode='real'
    """)
    c.commit(); c.close()

    set_meta("last_reset_date", today)
    set_meta("first_start_logged_date", "")

# =========================
# TIMER LOGIC
# =========================
def timer_total_seconds(mode, i, n):
    c = db(); cur = c.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    c.close()

    total = int(t["elapsed"])
    if int(t["running"]) != 1:
        return total

    if mode == "real":
        return total + int(tz_now_real().timestamp() - float(t["start_epoch"]))
    else:
        start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
        return total + int((n - start).total_seconds())

def fmt(sec):
    return f"{sec//3600:02}:{(sec%3600)//60:02}:{sec%60:02}"

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
        ]
    )
    gc = gspread.authorize(creds)
    WS = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def target_hour_and_date(n):
    if n.hour < FIRST_LOG_HOUR:
        return 23, n.date() - timedelta(days=1)
    return min(n.hour, 23), n.date()

def log_to_sheet(force=False):
    ws = gs()
    if not ws:
        return False, "no creds"

    n = tz_now_real()
    hour, day = target_hour_and_date(n)

    if not force:
        if get_meta("last_logged_hour") == str(hour) and get_meta("last_logged_day") == day.isoformat():
            return True, "skip"

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return False, "date not found"

    col = headers.index(date_str) + 1
    row = max(7, min(22, 7 + hour - 8))

    total = sum(timer_total_seconds("real", i, tz_now_real()) for i in range(1, TIMER_COUNT + 1))
    ws.update_cell(row, col, fmt(total))

    set_meta("last_logged_hour", str(hour))
    set_meta("last_logged_day", day.isoformat())
    return True, "logged"

def maybe_auto_log():
    n = tz_now_real()
    if n.minute == 0 and n.second == 0 and FIRST_LOG_HOUR <= n.hour <= LAST_LOG_HOUR:
        log_to_sheet(False)

# =========================
# START TIME (ROW 4)
# =========================
def log_start_time_if_needed(n):
    if n.hour < RESET_HOUR:
        return

    ws = gs()
    if not ws:
        return

    _, day = target_hour_and_date(n)
    key = day.isoformat()
    if get_meta("first_start_logged_date") == key:
        return

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    ws.update_cell(4, col, n.strftime("%H:%M"))
    set_meta("first_start_logged_date", key)

# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    ensure_daily_reset()
    maybe_auto_log()

    n = now()
    mode = current_mode()
    timers = [fmt(timer_total_seconds(mode, i, n)) for i in range(1, TIMER_COUNT + 1)]

    return jsonify(
        now_str=n.strftime("%d/%m/%Y %H:%M:%S"),
        simulation=sim_enabled(),
        timers=timers
    )

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    n = now()
    log_start_time_if_needed(n)

    c = db(); cur = c.cursor()
    cur.execute("SELECT running FROM timers WHERE mode=? AND timer_id=?", (current_mode(), i))
    if cur.fetchone()["running"] == 0:
        if current_mode() == "real":
            cur.execute("""
            UPDATE timers SET running=1, start_epoch=?
            WHERE mode='real' AND timer_id=?
            """, (tz_now_real().timestamp(), i))
        else:
            cur.execute("""
            UPDATE timers SET running=1, start_sim_iso=?
            WHERE mode='sim' AND timer_id=?
            """, (n.replace(tzinfo=None).isoformat(timespec="seconds"), i))
    c.commit(); c.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    n = now()
    total = timer_total_seconds(current_mode(), i, n)

    c = db(); cur = c.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (total, current_mode(), i))
    c.commit(); c.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    c = db(); cur = c.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (current_mode(), i))
    c.commit(); c.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/adjust", methods=["POST"])
def adjust_timer(i):
    data = request.get_json()
    delta = int(data["delta"])

    c = db(); cur = c.cursor()
    cur.execute("SELECT running, elapsed FROM timers WHERE mode=? AND timer_id=?", (current_mode(), i))
    t = cur.fetchone()

    if t["running"] == 1:
        return jsonify(error="cannot edit while running"), 400

    new_val = max(0, int(t["elapsed"]) + delta)
    cur.execute("""
    UPDATE timers SET elapsed=? WHERE mode=? AND timer_id=?
    """, (new_val, current_mode(), i))
    c.commit(); c.close()

    return jsonify(ok=True, new_time=fmt(new_val))

@app.route("/api/log-now", methods=["POST"])
def manual_log():
    ok, msg = log_to_sheet(True)
    return jsonify(ok=ok, message=msg)

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    dt = TZ.localize(datetime.strptime(request.json["datetime"], "%Y-%m-%d %H:%M"))
    set_meta("sim_enabled", "1")
    set_sim_now(dt)
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    set_meta("sim_enabled", "0")
    set_meta("sim_now_iso", "")
    return jsonify(ok=True)

if __name__ == "__main__":
    app.run(debug=True)