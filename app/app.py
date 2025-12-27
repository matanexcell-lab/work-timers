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
app = Flask(__name__)

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

# סימולציה
use_simulation = False
simulated_now = None

# =========================
# TIME HELPERS
# =========================
def now():
    if use_simulation and simulated_now:
        return simulated_now
    return datetime.now(TZ)

def seconds_to_hms(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def effective_seconds(timer, dt):
    sec = timer["accum"]
    if timer["running"] and timer["start"]:
        sec += int((dt - timer["start"]).total_seconds())
    return sec

def workday_key(dt):
    cutoff = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

# =========================
# GOOGLE SHEET WRITE
# =========================
def find_or_create_date_column(date_str):
    header = WS.row_values(3)
    if date_str in header:
        return header.index(date_str) + 1

    col = len(header) + 1
    WS.update_cell(3, col, date_str)
    WS.update_cell(3, col + 1, date_str)
    return col

def write_hour(hour):
    date_str = workday_key(now())
    col = find_or_create_date_column(date_str)

    row = 7 + (hour - FIRST_HOUR)

    v1 = seconds_to_hms(effective_seconds(timers[0], now()))
    v2 = seconds_to_hms(effective_seconds(timers[1], now()))

    WS.update_cell(row, col, v1)
    WS.update_cell(row, col + 1, v2)

    return [v1, v2]

# =========================
# BACKGROUND WORKER
# =========================
def background_worker():
    global last_logged_hour, current_workday

    while True:
        dt = now()
        wd = workday_key(dt)

        if current_workday != wd:
            current_workday = wd
            last_logged_hour = None
            for t in timers:
                t["running"] = False
                t["start"] = None
                t["accum"] = 0

        if (
            dt.minute == 0
            and FIRST_HOUR <= dt.hour <= LAST_HOUR
            and dt.hour != last_logged_hour
        ):
            write_hour(dt.hour)
            last_logged_hour = dt.hour

        time.sleep(30)

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "now": dt.strftime("%d/%m/%Y %H:%M:%S"),
        "simulated": use_simulation,
        "timers": [
            seconds_to_hms(effective_seconds(timers[i], dt))
            for i in range(TIMER_COUNT)
        ]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    t = timers[i - 1]
    if not t["running"]:
        t["running"] = True
        t["start"] = now()
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    t = timers[i - 1]
    if t["running"]:
        t["accum"] += int((now() - t["start"]).total_seconds())
        t["running"] = False
        t["start"] = None
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers[i - 1] = {"running": False, "start": None, "accum": 0}
    return jsonify(ok=True)

@app.route("/api/log-now", methods=["POST"])
def log_now():
    values = write_hour(now().hour)
    return jsonify(logged=True, values=values)

@app.route("/api/simulate", methods=["POST"])
def simulate():
    global use_simulation, simulated_now
    data = request.json
    use_simulation = data["enabled"]
    if use_simulation:
        simulated_now = TZ.localize(
            datetime.strptime(
                data["date"] + " " + data["time"],
                "%Y-%m-%d %H:%M"
            )
        )
    return jsonify(ok=True)

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    current_workday = workday_key(now())
    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)