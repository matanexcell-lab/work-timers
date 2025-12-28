import os
import json
import threading
import time
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, render_template, request

# ======================
# APP
# ======================
app = Flask(__name__, template_folder="templates")
TZ = pytz.timezone("Asia/Jerusalem")

# ======================
# CONFIG
# ======================
TIMER_COUNT = 2
RESET_HOUR = 5
FIRST_LOG_HOUR = 8
LAST_LOG_HOUR = 24

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# ======================
# GOOGLE SHEETS
# ======================
def gs_connect():
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.getenv("GOOGLE_CREDS_JSON")
    info = json.loads(raw)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

WS = gs_connect()

# ======================
# STATE
# ======================
def new_timer():
    return {"running": False, "accum": 0}

timers_real = [new_timer() for _ in range(TIMER_COUNT)]
timers_sim = [new_timer() for _ in range(TIMER_COUNT)]

simulation = {"enabled": False, "now": None}

last_reset_date = None
last_logged_hour = None
first_start_logged_date = None

lock = threading.Lock()

# ======================
# TIME HELPERS
# ======================
def now():
    if simulation["enabled"]:
        return simulation["now"]
    return datetime.now(TZ)

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

def active_timers():
    return timers_sim if simulation["enabled"] else timers_real

# ======================
# DAILY RESET
# ======================
def check_daily_reset():
    global last_reset_date, first_start_logged_date
    n = now()

    if n.hour >= RESET_HOUR:
        if last_reset_date != n.date():
            for t in timers_real:
                t["running"] = False
                t["accum"] = 0
            last_reset_date = n.date()
            first_start_logged_date = None

# ======================
# GOOGLE SHEET LOGIC
# ======================
def target_hour_and_date(n):
    if n.hour < FIRST_LOG_HOUR:
        return 23, n.date() - timedelta(days=1)
    return min(n.hour, 24), n.date()

def log_to_sheet(force=False):
    global last_logged_hour

    n = now()
    hour, day = target_hour_and_date(n)

    if not force and hour == last_logged_hour:
        return

    headers = WS.row_values(3)
    date_str = day.strftime("%d/%m/%Y")
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    row = 7 + (hour - 8)

    total = sum(t["accum"] for t in timers_real)
    WS.update_cell(row, col, fmt(total))

    last_logged_hour = hour

# ======================
# BACKGROUND THREAD
# ======================
def bg_loop():
    while True:
        with lock:
            check_daily_reset()

            for t in timers_real:
                if t["running"]:
                    t["accum"] += 1

            n = now()
            if n.minute == 0 and n.second == 0:
                if FIRST_LOG_HOUR <= n.hour <= LAST_LOG_HOUR:
                    log_to_sheet()

        time.sleep(1)

threading.Thread(target=bg_loop, daemon=True).start()

# ======================
# ROUTES
# ======================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    n = now()
    ts = active_timers()
    return jsonify({
        "now_str": n.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation["enabled"],
        "timers": [fmt(t["accum"]) for t in ts]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    global first_start_logged_date
    with lock:
        check_daily_reset()
        t = active_timers()[i-1]
        t["running"] = True

        if not simulation["enabled"]:
            n = now()
            if n.hour >= RESET_HOUR and first_start_logged_date != n.date():
                headers = WS.row_values(3)
                date_str = n.strftime("%d/%m/%Y")
                if date_str in headers:
                    col = headers.index(date_str) + 1
                    WS.update_cell(4, col, n.strftime("%H:%M"))
                    first_start_logged_date = n.date()
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    with lock:
        active_timers()[i-1]["running"] = False
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    with lock:
        active_timers()[i-1].update(running=False, accum=0)
    return jsonify(ok=True)

@app.route("/api/log", methods=["POST"])
def manual_log():
    with lock:
        log_to_sheet(force=True)
    return jsonify(ok=True)

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    data = request.json
    simulation["enabled"] = True
    simulation["now"] = TZ.localize(
        datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    )
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    simulation["enabled"] = False
    simulation["now"] = None
    return jsonify(ok=True)

if __name__ == "__main__":
    app.run(debug=True)