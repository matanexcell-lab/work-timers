# app.py
import os
import json
import sqlite3
import traceback
from datetime import datetime, timedelta, date, time as dtime

import pytz
from flask import Flask, jsonify, render_template, request

# =========================
# APP
# =========================
app = Flask(__name__, template_folder="templates")

# =========================
# TIMEZONE
# =========================
TZ = pytz.timezone("Asia/Jerusalem")

def tz_now_real():
    return datetime.now(TZ)

# =========================
# CONFIG
# =========================
TIMER_COUNT = 2
RESET_HOUR = 5
FIRST_LOG_HOUR = 8

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

ALLOW_EDIT_WHILE_RUNNING = True

CALENDAR_SUMMARY = os.getenv("CALENDAR_SUMMARY", "סיכום יום")
CALENDAR_ID = os.getenv("CALENDAR_ID", "").strip()

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
        "last_auto_logged_key_real": "",
        "last_auto_logged_key_sim": "",
        "last_reset_date_real": "",
        "last_reset_date_sim": "",
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",
        "last_sheet_ok": "0",
        "last_sheet_msg": "",
        "last_sheet_at": "",
        "last_cal_ok": "0",
        "last_cal_msg": "",
        "last_cal_at": "",
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
# MODE / CLOCK
# =========================
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

def current_mode():
    return "sim" if sim_enabled() else "real"

def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()

# =========================
# TIMER CORE
# =========================
def timer_row(mode, i):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    r = cur.fetchone()
    conn.close()
    return r

def timer_seconds(mode, i, now_dt):
    t = timer_row(mode, i)
    if not t:
        return 0

    elapsed = int(t["elapsed"])
    if not t["running"]:
        return elapsed

    if mode == "real":
        return elapsed + int(tz_now_real().timestamp() - t["start_epoch"])

    start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    return elapsed + int((now_dt - start).total_seconds())

def fmt(sec):
    sec = max(0, int(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02}"

# =========================
# UI
# =========================
@app.route("/")
def ui():
    return render_template("index.html")

# =========================
# API STATUS (כל שנייה)
# =========================
@app.route("/api/status")
def status():
    if sim_enabled():
        set_sim_now(get_sim_now() + timedelta(seconds=1))

    mode = current_mode()
    clock = now_for_mode(mode)

    timers = [fmt(timer_seconds(mode, i, clock)) for i in range(1, TIMER_COUNT + 1)]

    return jsonify({
        "now_str": clock.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled(),
        "timers": timers,
        "sheet": {
            "ok": get_meta("last_sheet_ok") == "1",
            "msg": get_meta("last_sheet_msg"),
            "at": get_meta("last_sheet_at"),
        },
        "calendar": {
            "ok": get_meta("last_cal_ok") == "1",
            "msg": get_meta("last_cal_msg"),
            "at": get_meta("last_cal_at"),
        }
    })

# =========================
# TIMER CONTROLS
# =========================
@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    mode = current_mode()
    now = now_for_mode(mode)

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
        """, (now.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))

    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    mode = current_mode()
    now = now_for_mode(mode)
    total = timer_seconds(mode, i, now)

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
    UPDATE timers SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (mode, i))
    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/adjust", methods=["POST"])
def adjust_timer(i):
    delta = int(request.json.get("delta", 0))
    mode = current_mode()
    now = now_for_mode(mode)
    total = timer_seconds(mode, i, now) + delta

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers SET elapsed=?, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (max(0, total), mode, i))
    conn.commit()
    conn.close()
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/set", methods=["POST"])
def set_timer(i):
    sec = int(request.json.get("seconds", 0))
    mode = current_mode()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE timers SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL
    WHERE mode=? AND timer_id=?
    """, (max(0, sec), mode, i))
    conn.commit()
    conn.close()
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
# MAIN
# =========================
if __name__ == "__main__":
    app.run(debug=True)