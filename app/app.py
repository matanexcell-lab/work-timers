import os
import json
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, render_template, request

# =========================
# CONFIG
# =========================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
FIRST_HOUR = 8
LAST_HOUR = 23          # 23 = 23:00–24:00
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

# =========================
# STATE (IN MEMORY)
# =========================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(TIMER_COUNT)
]

last_logged_hour = None
current_workday = None

# =========================
# HELPERS
# =========================
def now():
    return datetime.now(TZ)

def seconds_to_hms(sec: int) -> str:
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
def write_hour(hour):
    WS = gs_connect()

    date_str = current_workday
    row = 7 + (hour - FIRST_HOUR)

    # תאריך
    WS.update_cell(3, 2, date_str)
    WS.update_cell(3, 3, date_str)

    values = [
        seconds_to_hms(effective_seconds(timers[i], now()))
        for i in range(TIMER_COUNT)
    ]

    WS.update_cell(row, 2, values[0])
    WS.update_cell(row, 3, values[1])

    return values

# =========================
# AUTO HOURLY CHECK
# =========================
def hourly_check():
    global last_logged_hour, current_workday

    dt = now()
    wd = workday_key(dt)

    # reset יומי
    if current_workday != wd:
        current_workday = wd
        last_logged_hour = None
        for t in timers:
            t["running"] = False
            t["start"] = None
            t["accum"] = 0

    # שעה עגולה
    if (
        dt.minute == 0
        and FIRST_HOUR <= dt.hour <= LAST_HOUR
        and dt.hour != last_logged_hour
    ):
        write_hour(dt.hour)
        last_logged_hour = dt.hour

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "✅ Work Timers is running"

@app.route("/ui")
def ui():
    hourly_check()
    return render_template("index.html")

@app.route("/api/status")
def status():
    hourly_check()
    dt = now()
    return jsonify({
        "workday": current_workday,
        "timers": [
            seconds_to_hms(effective_seconds(timers[i], dt))
            for i in range(TIMER_COUNT)
        ]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    hourly_check()
    if 1 <= i <= TIMER_COUNT:
        t = timers[i - 1]
        if not t["running"]:
            t["running"] = True
            t["start"] = now()
        return jsonify({"status": "started", "timer": i})
    return jsonify({"error": "invalid timer"}), 400

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    hourly_check()
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
    hourly_check()
    if 1 <= i <= TIMER_COUNT:
        timers[i - 1] = {"running": False, "start": None, "accum": 0}
        return jsonify({"status": "reset", "timer": i})
    return jsonify({"error": "invalid timer"}), 400

@app.route("/api/log-now", methods=["POST"])
def log_now():
    hourly_check()
    if not (FIRST_HOUR <= now().hour <= LAST_HOUR):
        return jsonify({"error": "outside logging hours"}), 400
    values = write_hour(now().hour)
    return jsonify({"logged": True, "values": values})

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    current_workday = workday_key(now())
    app.run(host="0.0.0.0", port=5000, debug=False)