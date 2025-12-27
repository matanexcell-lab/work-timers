import os
import json
import time
import threading
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, render_template, request

# ==================================================
# CONFIG
# ==================================================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
FIRST_HOUR = 8
LAST_HOUR = 23
RESET_HOUR = 5  # 05:00 איפוס יומי

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# ==================================================
# FLASK
# ==================================================
app = Flask(__name__)

# ==================================================
# GOOGLE SHEETS
# ==================================================
def gs_connect():
    import gspread
    from google.oauth2.service_account import Credentials

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

# ==================================================
# STATE
# ==================================================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(TIMER_COUNT)
]

last_logged_hour = None
current_workday = None
start_logged_for_day = False

# ==================================================
# TIME (אמיתי / סימולציה)
# ==================================================
simulation_enabled = False
simulation_dt = None

def now():
    if simulation_enabled and simulation_dt:
        return simulation_dt
    return datetime.now(TZ)

def set_simulation(date_str, time_str):
    global simulation_enabled, simulation_dt
    simulation_enabled = True
    simulation_dt = TZ.localize(
        datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M")
    )

def disable_simulation():
    global simulation_enabled, simulation_dt
    simulation_enabled = False
    simulation_dt = None

# ==================================================
# HELPERS
# ==================================================
def seconds_to_hms(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def effective_seconds(timer, dt):
    sec = timer["accum"]
    if timer["running"] and timer["start"]:
        sec += int((dt - timer["start"]).total_seconds())
    return max(sec, 0)

def workday_key(dt):
    cutoff = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

# ==================================================
# GOOGLE SHEET HELPERS
# ==================================================
def find_date_col(date_str):
    row = WS.row_values(3)
    for i, val in enumerate(row, start=1):
        if val.strip() == date_str:
            return i
    raise RuntimeError(f"Date {date_str} not found in row 3")

def write_start_time_if_needed(start_dt):
    global start_logged_for_day

    if start_logged_for_day:
        return

    col = find_date_col(current_workday)
    WS.update_cell(4, col, start_dt.strftime("%H:%M"))

    start_logged_for_day = True
    print(f"🕗 Start time logged: {start_dt.strftime('%H:%M')}")

def write_hour(hour):
    col = find_date_col(current_workday)
    row = 7 + (hour - FIRST_HOUR)

    values = [
        seconds_to_hms(effective_seconds(timers[i], now()))
        for i in range(TIMER_COUNT)
    ]

    WS.update_cell(row, col, values[0])
    WS.update_cell(row, col + 1, values[1])

    return values

# ==================================================
# BACKGROUND WORKER
# ==================================================
def background_worker():
    global current_workday, last_logged_hour, start_logged_for_day

    while True:
        dt = now()
        wd = workday_key(dt)

        # 🔄 איפוס יומי ב־05:00
        if current_workday != wd:
            current_workday = wd
            last_logged_hour = None
            start_logged_for_day = False

            for t in timers:
                t["running"] = False
                t["start"] = None
                t["accum"] = 0

            print("🔄 Daily reset (05:00)")

        # 🕒 רישום שעה עגולה
        if (
            dt.minute == 0
            and FIRST_HOUR <= dt.hour <= LAST_HOUR
            and dt.hour != last_logged_hour
        ):
            write_hour(dt.hour)
            last_logged_hour = dt.hour
            print(f"📝 Logged hour {dt.hour}")

        time.sleep(30)

# ==================================================
# ROUTES
# ==================================================
@app.route("/")
def home():
    return render_template(
        "index.html",
        now_str=now().strftime("%d/%m/%Y %H:%M"),
        sim=simulation_enabled
    )

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "now": dt.strftime("%d/%m/%Y %H:%M"),
        "workday": current_workday,
        "simulation": simulation_enabled,
        "timers": [
            seconds_to_hms(effective_seconds(timers[i], dt))
            for i in range(TIMER_COUNT)
        ]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if 1 <= i <= TIMER_COUNT:
        t = timers[i - 1]
        if not t["running"]:
            t["running"] = True
            t["start"] = now()
            write_start_time_if_needed(t["start"])
        return jsonify({"status": "started", "timer": i})
    return jsonify({"error": "invalid timer"}), 400

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if 1 <= i <= TIMER_COUNT:
        t = timers[i - 1]
        if t["running"]:
            t["accum"] += int((now() - t["start"]).total_seconds())
            t["running"] = False
            t["start"] = None
        return jsonify({"status": "stopped", "timer": i})
    return jsonify({"error": "invalid timer"}), 400

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    if 1 <= i <= TIMER_COUNT:
        timers[i - 1] = {"running": False, "start": None, "accum": 0}
        return jsonify({"status": "reset", "timer": i})
    return jsonify({"error": "invalid timer"}), 400

@app.route("/api/log-now", methods=["POST"])
def log_now():
    dt = now()
    if not (FIRST_HOUR <= dt.hour <= LAST_HOUR):
        return jsonify({"error": "outside logging hours"}), 400
    values = write_hour(dt.hour)
    return jsonify({"logged": True, "values": values})

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    data = request.json
    set_simulation(data["date"], data["time"])
    return jsonify({"simulation": True})

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    disable_simulation()
    return jsonify({"simulation": False})

# ==================================================
# MAIN
# ==================================================
if __name__ == "__main__":
    current_workday = workday_key(now())
    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)