import os, json, time, threading
from datetime import datetime, timedelta
import pytz

from flask import Flask, jsonify, request, render_template
import gspread
from google.oauth2.service_account import Credentials

# =========================
# CONFIG
# =========================
TZ = pytz.timezone("Asia/Jerusalem")
RESET_HOUR = 5
TIMER_COUNT = 2

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# =========================
# APP
# =========================
app = Flask(__name__)

# =========================
# GOOGLE SHEETS
# =========================
def get_ws():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    info = json.loads(raw)

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# =========================
# STATE
# =========================
timers = [{"running": False, "start": None, "accum": 0} for _ in range(TIMER_COUNT)]

simulation_on = False
simulation_now = None

last_reset_day = None
start_logged_day = None

# =========================
# TIME
# =========================
def now():
    if simulation_on and simulation_now:
        return simulation_now
    return datetime.now(TZ)

# =========================
# DAILY RESET
# =========================
def check_daily_reset(t):
    global last_reset_day, start_logged_day

    day_key = t.strftime("%Y-%m-%d")

    if t.hour >= RESET_HOUR and last_reset_day != day_key:
        for tm in timers:
            tm["running"] = False
            tm["start"] = None
            tm["accum"] = 0

        last_reset_day = day_key
        start_logged_day = None
        print("🔄 Reset at 05:00")

# =========================
# START TIME LOG
# =========================
def log_start_time(t):
    global start_logged_day

    day_key = t.strftime("%Y-%m-%d")
    if start_logged_day == day_key:
        return

    ws = get_ws()
    date_str = t.strftime("%d/%m/%Y")
    time_str = t.strftime("%H:%M")

    dates = ws.row_values(3)
    if date_str not in dates:
        print("❌ Date not found:", date_str)
        return

    col = dates.index(date_str) + 1
    ws.update_cell(4, col, time_str)

    start_logged_day = day_key
    print("🕔 Start logged:", date_str, time_str)

# =========================
# BACKGROUND TICK
# =========================
def ticker():
    while True:
        t = now()
        check_daily_reset(t)

        for tm in timers:
            if tm["running"]:
                tm["accum"] += 1

        time.sleep(1)

threading.Thread(target=ticker, daemon=True).start()

# =========================
# HELPERS
# =========================
def fmt(sec):
    return str(timedelta(seconds=sec))

# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    t = now()
    return jsonify({
        "now_str": t.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation_on,
        "timers": [fmt(tm["accum"]) for tm in timers]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    t = now()
    idx = i - 1

    if not timers[idx]["running"]:
        if t.hour >= RESET_HOUR:
            log_start_time(t)

        timers[idx]["running"] = True
        timers[idx]["start"] = t

    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    timers[i-1]["running"] = False
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers[i-1]["running"] = False
    timers[i-1]["start"] = None
    timers[i-1]["accum"] = 0
    return jsonify(ok=True)

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    global simulation_on, simulation_now
    data = request.json
    simulation_now = TZ.localize(
        datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    )
    simulation_on = True
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    global simulation_on, simulation_now
    simulation_on = False
    simulation_now = None
    return jsonify(ok=True)

@app.route("/api/log", methods=["POST"])
def log_google():
    ws = get_ws()
    t = now()
    date_str = t.strftime("%d/%m/%Y")
    dates = ws.row_values(3)

    if date_str not in dates:
        return jsonify(error="date not found")

    col = dates.index(date_str) + 1

    for i, tm in enumerate(timers):
        ws.update_cell(7 + i, col, fmt(tm["accum"]))

    return jsonify(logged=True)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run()