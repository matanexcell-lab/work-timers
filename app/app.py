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
    cur.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))
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

def now():
    return get_sim_now() if sim_enabled() else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# TIMER CORE
# =========================
def timer_total_seconds(mode, timer_id, n):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, timer_id))
    t = cur.fetchone()
    conn.close()

    total = t["elapsed"]
    if not t["running"]:
        return total

    if mode == "real":
        return total + int(tz_now_real().timestamp() - t["start_epoch"])

    start_dt = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    return total + int((n - start_dt).total_seconds())

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

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
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    n = now()
    if sim_enabled():
        set_sim_now(n + timedelta(seconds=1))
        n = get_sim_now()

    ensure_daily_reset()

    timers = [
        fmt(timer_total_seconds(current_mode(), i, n))
        for i in range(1, TIMER_COUNT + 1)
    ]

    return jsonify({
        "now_str": n.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled(),
        "timers": timers
    })

# =========================
# START / STOP / RESET
# =========================
@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    mode = current_mode()
    n = now()

    conn = db()
    cur = conn.cursor()
    if mode == "real":
        cur.execute("""
        UPDATE timers SET running=1, start_epoch=?
        WHERE mode=? AND timer_id=?
        """, (tz_now_real().timestamp(), mode, i))
    else:
        cur.execute("""
        UPDATE timers SET running=1, start_sim_iso=?
        WHERE mode=? AND timer_id=?
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
    WHERE mode=? AND timer_id=?
    """, (current_mode(), i))
    conn.commit()
    conn.close()
    return ("", 204)

# =========================
# ⭐ EDIT TIME (NEW)
# =========================
@app.route("/api/timer/<int:i>/set", methods=["POST"])
def set_timer(i):
    data = request.get_json(force=True)
    seconds = max(0, int(data.get("seconds", 0)))

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers
    SET elapsed=?, running=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (seconds, current_mode(), i))
    conn.commit()
    conn.close()

    return jsonify(ok=True, time=fmt(seconds))

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

if __name__ == "__main__":
    app.run(debug=True)