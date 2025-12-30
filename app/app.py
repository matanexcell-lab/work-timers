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
        "first_start_logged_day": ""
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (k, v))

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
def real_now():
    return datetime.now(TZ)

def sim_enabled():
    return get_meta("sim_enabled") == "1"

def get_sim_now():
    iso = get_meta("sim_now_iso")
    if not iso:
        return None
    return TZ.localize(datetime.fromisoformat(iso))

def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat())

def now():
    return get_sim_now() if sim_enabled() else real_now()

def mode():
    return "sim" if sim_enabled() else "real"

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

# =========================
# RESET 05:00
# =========================
def daily_reset():
    n = real_now()
    if n.hour < RESET_HOUR:
        return

    today = n.date().isoformat()
    if get_meta("last_reset_date") == today:
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=0, start_epoch=NULL
    WHERE mode='real'
    """)
    conn.commit()
    conn.close()

    set_meta("last_reset_date", today)
    set_meta("first_start_logged_day", "")

# =========================
# TIMER CORE
# =========================
def total_seconds(mode, timer_id, n):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, timer_id))
    t = cur.fetchone()
    conn.close()

    sec = t["elapsed"]
    if not t["running"]:
        return sec

    if mode == "real":
        return sec + int(real_now().timestamp() - t["start_epoch"])

    start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    return sec + int((n - start).total_seconds())

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
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ])
    WS = gspread.authorize(creds).open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def log_to_sheet(force=False):
    ws = gs()
    if not ws:
        return False

    n = real_now()
    hour = 23 if n.hour < FIRST_LOG_HOUR else min(n.hour, 23)
    day = n.date() if n.hour >= FIRST_LOG_HOUR else n.date() - timedelta(days=1)

    if not force:
        if get_meta("last_logged_hour") == str(hour) and get_meta("last_logged_day") == day.isoformat():
            return True

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return False

    col = headers.index(date_str) + 1
    row = 7 + (hour - 8)
    row = max(7, min(row, 22))

    total = sum(total_seconds("real", i, n) for i in range(1, TIMER_COUNT + 1))
    ws.update_cell(row, col, fmt(total))

    set_meta("last_logged_hour", str(hour))
    set_meta("last_logged_day", day.isoformat())
    return True

# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    daily_reset()

    if sim_enabled():
        set_sim_now(now() + timedelta(seconds=1))

    timers = [fmt(total_seconds(mode(), i, now())) for i in range(1, TIMER_COUNT + 1)]

    if real_now().minute == 0 and real_now().second == 0:
        if FIRST_LOG_HOUR <= real_now().hour <= LAST_LOG_HOUR:
            log_to_sheet()

    return jsonify({
        "now_str": now().strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled(),
        "timers": timers
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start(i):
    n = now()
    m = mode()

    conn = db()
    cur = conn.cursor()

    if m == "real":
        cur.execute("UPDATE timers SET running=1, start_epoch=? WHERE mode='real' AND timer_id=?",
                    (real_now().timestamp(), i))

        today = real_now().date().isoformat()
        if get_meta("first_start_logged_day") != today:
            ws = gs()
            if ws:
                date_str = real_now().strftime("%d/%m/%Y")
                headers = ws.row_values(3)
                if date_str in headers:
                    ws.update_cell(4, headers.index(date_str)+1, real_now().strftime("%H:%M"))
            set_meta("first_start_logged_day", today)

    else:
        cur.execute("UPDATE timers SET running=1, start_sim_iso=? WHERE mode='sim' AND timer_id=?",
                    (n.replace(tzinfo=None).isoformat(), i))

    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop(i):
    n = now()
    m = mode()
    sec = total_seconds(m, i, n)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (sec, m, i))
    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset(i):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (mode(), i))
    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/set-time", methods=["POST"])
def set_time(i):
    t = request.json.get("time")
    h, m, s = map(int, t.split(":"))
    sec = h*3600 + m*60 + s

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (sec, mode(), i))
    conn.commit()
    conn.close()
    return jsonify(ok=True)

@app.route("/api/log-now", methods=["POST"])
def log_now():
    return jsonify(ok=log_to_sheet(force=True))

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