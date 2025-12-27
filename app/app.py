import os
import json
import time
import threading
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, render_template, request

# =========================
# CONFIG
# =========================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
FIRST_HOUR = 8
LAST_HOUR = 23
RESET_HOUR = 5

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# =========================
# FLASK
# =========================
app = Flask(__name__, template_folder="templates")

# =========================
# GOOGLE SHEETS
# =========================
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

# =========================
# STATE
# =========================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(TIMER_COUNT)
]

last_logged_hour = None
current_workday = None
first_start_time = None

# --- סימולציה ---
simulation_enabled = False
simulated_now = None

# =========================
# TIME HELPERS
# =========================
def get_now():
    if simulation_enabled and simulated_now is not None:
        return simulated_now
    return datetime.now(TZ)

def advance_simulation(seconds=1):
    global simulated_now
    if simulated_now:
        simulated_now += timedelta(seconds=seconds)

def seconds_to_hms(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def effective_seconds(timer, now_dt):
    sec = timer["accum"]
    if timer["running"] and timer["start"]:
        sec += int((now_dt - timer["start"]).total_seconds())
    return sec

def workday_key(dt):
    cutoff = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

# =========================
# GOOGLE SHEET HELPERS
# =========================
def find_or_create_date_column(date_str):
    header = WS.row_values(3)
    if date_str in header:
        return header.index(date_str) + 1

    col = len(header) + 1
    WS.update_cell(3, col, date_str)
    WS.update_cell(4, col, "התחלה")
    return col

def write_start_time_to_sheet(dt):
    date_str = workday_key(dt)
    col = find_or_create_date_column(date_str)
    WS.update_cell(4, col, dt.strftime("%H:%M"))

def write_hour(hour, now_dt):
    date_str = workday_key(now_dt)
    col = find_or_create_date_column(date_str)

    row = 7 + (hour - FIRST_HOUR)
    values = [
        seconds_to_hms(effective_seconds(timers[i], now_dt))
        for i in range(TIMER_COUNT)
    ]

    for i, v in enumerate(values):
        WS.update_cell(row, col + i, v)

    return values

# =========================
# BACKGROUND WORKER
# =========================
def background_worker():
    global last_logged_hour, current_workday, first_start_time

    while True:
        now_dt = get_now()
        wd = workday_key(now_dt)

        # איפוס יומי ב־05:00
        if current_workday != wd:
            current_workday = wd
            last_logged_hour = None
            first_start_time = None
            for t in timers:
                t["running"] = False
                t["start"] = None
                t["accum"] = 0
            print("🔄 Daily reset")

        # רישום שעה עגולה
        if (
            now_dt.minute == 0
            and FIRST_HOUR <= now_dt.hour <= LAST_HOUR
            and now_dt.hour != last_logged_hour
        ):
            write_hour(now_dt.hour, now_dt)
            last_logged_hour = now_dt.hour
            print(f"📝 Logged hour {now_dt.hour}")

        # קידום סימולציה
        if simulation_enabled:
            advance_simulation(1)

        time.sleep(1)

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "✅ Work Timers is running"

@app.route("/ui")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    now_dt = get_now()
    return jsonify({
        "now": now_dt.strftime("%d/%m/%Y %H:%M:%S"),
        "workday": current_workday,
        "simulation": simulation_enabled,
        "timers": [
            seconds_to_hms(effective_seconds(timers[i], now_dt))
            for i in range(TIMER_COUNT)
        ]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    global first_start_time

    if not (1 <= i <= TIMER_COUNT):
        return jsonify({"error": "invalid timer"}), 400

    now_dt = get_now()
    t = timers[i - 1]

    if not t["running"]:
        t["running"] = True
        t["start"] = now_dt

        if first_start_time is None:
            first_start_time = now_dt
            write_start_time_to_sheet(now_dt)

    return jsonify({"status": "started", "timer": i})

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if not (1 <= i <= TIMER_COUNT):
        return jsonify({"error": "invalid timer"}), 400

    now_dt = get_now()
    t = timers[i - 1]

    if t["running"]:
        t["accum"] += int((now_dt - t["start"]).total_seconds())
        t["running"] = False
        t["start"] = None

    return jsonify({"status": "stopped", "timer": i})

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    if not (1 <= i <= TIMER_COUNT):
        return jsonify({"error": "invalid timer"}), 400

    timers[i - 1] = {"running": False, "start": None, "accum": 0}
    return jsonify({"status": "reset", "timer": i})

@app.route("/api/log-now", methods=["POST"])
def log_now():
    now_dt = get_now()
    if not (FIRST_HOUR <= now_dt.hour <= LAST_HOUR):
        return jsonify({"error": "outside logging hours"}), 400

    values = write_hour(now_dt.hour, now_dt)
    return jsonify({"logged": True, "values": values})

# --- סימולציה ---
@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    global simulation_enabled, simulated_now
    data = request.json
    simulated_now = TZ.localize(datetime.strptime(
        data["datetime"], "%Y-%m-%d %H:%M"
    ))
    simulation_enabled = True
    return jsonify({"simulation": "started"})

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    global simulation_enabled, simulated_now
    simulation_enabled = False
    simulated_now = None
    return jsonify({"simulation": "stopped"})

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    current_workday = workday_key(get_now())
    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)