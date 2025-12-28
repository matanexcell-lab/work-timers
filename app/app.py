import os
import json
import threading
import time
from datetime import datetime, timedelta

import pytz
import gspread
from flask import Flask, jsonify, request
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
RESET_HOUR = 5
AUTO_LOG_HOURS = set(range(8, 25))  # 08–24

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# ================= APP =================
app = Flask(__name__)

# ================= GOOGLE SHEETS =================
def gs_connect():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDS_JSON")

    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open(SPREADSHEET_NAME)
    return sh.worksheet(WORKSHEET_NAME)

WS = gs_connect()

# ================= STATE =================
def new_timer():
    return {"running": False, "start": None, "accum": 0}

real_timers = [new_timer() for _ in range(TIMER_COUNT)]
sim_timers  = [new_timer() for _ in range(TIMER_COUNT)]

simulation = {
    "on": False,
    "now": None
}

last_logged_hour = None
current_workday = None
start_logged_for_day = False

lock = threading.Lock()

# ================= TIME =================
def now():
    if simulation["on"]:
        return simulation["now"]
    return datetime.now(TZ)

def workday_for(dt):
    return dt.date() if dt.hour >= RESET_HOUR else (dt - timedelta(days=1)).date()

# ================= SHEET HELPERS =================
def col_for_hour(h):
    if h == 24:
        return 17
    return h - 7

def find_date_row(d):
    vals = WS.col_values(1)
    s = d.strftime("%d/%m/%Y")
    for i, v in enumerate(vals):
        if v.strip() == s:
            return i + 1
    WS.append_row([s])
    return len(vals) + 1

def write_start_time(dt):
    row = find_date_row(dt.date())
    WS.update_cell(row, 4, dt.strftime("%H:%M"))

def write_hours(dt, seconds):
    hour = dt.hour
    if hour < 8:
        hour = 24
    col = col_for_hour(hour)
    row = find_date_row(dt.date())
    prev = WS.cell(row, col).value
    prev = float(prev) if prev else 0
    WS.update_cell(row, col, round(prev + seconds / 3600, 2))

# ================= CORE LOGIC =================
def maybe_reset(dt):
    global current_workday, start_logged_for_day
    wd = workday_for(dt)
    if wd != current_workday:
        current_workday = wd
        start_logged_for_day = False
        for t in real_timers:
            t["running"] = False
            t["start"] = None
            t["accum"] = 0

def tick_loop():
    global last_logged_hour
    while True:
        time.sleep(1)
        with lock:
            dt = now()
            maybe_reset(dt)

            timers = sim_timers if simulation["on"] else real_timers

            for t in timers:
                if t["running"]:
                    t["accum"] += 1

            if not simulation["on"]:
                if dt.minute == 0 and dt.second == 0:
                    if dt.hour in AUTO_LOG_HOURS and last_logged_hour != dt.hour:
                        sec = sum(t["accum"] for t in real_timers)
                        write_hours(dt, sec)
                        last_logged_hour = dt.hour

threading.Thread(target=tick_loop, daemon=True).start()

# ================= API =================
@app.route("/")
def root():
    return "✅ Work Timers is running"

@app.route("/api/status")
def status():
    with lock:
        timers = sim_timers if simulation["on"] else real_timers
        out = []
        for t in timers:
            s = t["accum"]
            h, m, sec = s // 3600, (s % 3600) // 60, s % 60
            out.append(f"{h:02}:{m:02}:{sec:02}")

        return jsonify({
            "now_str": now().strftime("%d/%m/%Y %H:%M:%S"),
            "simulation": simulation["on"],
            "timers": out
        })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    idx = i - 1
    with lock:
        timers = sim_timers if simulation["on"] else real_timers
        t = timers[idx]
        if not t["running"]:
            t["running"] = True
            t["start"] = now()

            global start_logged_for_day
            if not simulation["on"] and not start_logged_for_day:
                write_start_time(now())
                start_logged_for_day = True
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    idx = i - 1
    with lock:
        timers = sim_timers if simulation["on"] else real_timers
        timers[idx]["running"] = False
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    idx = i - 1
    with lock:
        timers = sim_timers if simulation["on"] else real_timers
        timers[idx] = new_timer()
    return jsonify(ok=True)

@app.route("/api/log", methods=["POST"])
def manual_log():
    with lock:
        sec = sum(t["accum"] for t in real_timers)
        write_hours(now(), sec)
    return jsonify(ok=True)

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    data = request.json
    dt = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    simulation["on"] = True
    simulation["now"] = TZ.localize(dt)
    for t in sim_timers:
        t["running"] = False
        t["start"] = None
        t["accum"] = 0
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    simulation["on"] = False
    simulation["now"] = None
    return jsonify(ok=True)

# ================= POST FIX =================
@app.after_request
def add_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp