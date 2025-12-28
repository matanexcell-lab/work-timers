from flask import Flask, render_template, jsonify, request
from datetime import datetime, timedelta
import threading
import time
import pytz
import os
import json

# =====================
# APP
# =====================
app = Flask(__name__, template_folder="templates")
TZ = pytz.timezone("Asia/Jerusalem")

# =====================
# CONFIG
# =====================
TIMER_COUNT = 2
RESET_HOUR = 5
FIRST_LOG_HOUR = 8
LAST_LOG_HOUR = 24

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# =====================
# GOOGLE SHEETS (ENV VAR)
# =====================
WS = None
def gs_connect():
    global WS
    if WS:
        return
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    WS = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# =====================
# STATE
# =====================
def new_timer():
    return {"running": False, "start": None, "elapsed": 0}

timers_real = [new_timer() for _ in range(TIMER_COUNT)]
timers_sim  = [new_timer() for _ in range(TIMER_COUNT)]

simulation = {"enabled": False, "now": None}

last_reset_date = None
last_logged_hour = None
lock = threading.Lock()

# =====================
# TIME HELPERS
# =====================
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

# =====================
# DAILY RESET
# =====================
def check_daily_reset():
    global last_reset_date
    n = now()
    if n.hour >= RESET_HOUR:
        if last_reset_date != n.date():
            for t in timers_real:
                t.update(new_timer())
            last_reset_date = n.date()

# =====================
# GOOGLE SHEET LOGIC
# =====================
def target_hour_and_date(n):
    if n.hour < FIRST_LOG_HOUR:
        return 23, n.date() - timedelta(days=1)
    if n.hour > 23:
        return 23, n.date()
    return n.hour, n.date()

def log_to_sheet(force=False):
    global last_logged_hour
    if not WS:
        return

    n = now()
    hour, day = target_hour_and_date(n)

    if not force and hour == last_logged_hour:
        return

    date_str = day.strftime("%d/%m/%Y")
    headers = WS.row_values(3)
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    row = 7 + (hour - 8)

    total = sum(t["elapsed"] for t in timers_real)
    WS.update_cell(row, col, fmt(total))
    last_logged_hour = hour

# =====================
# BACKGROUND LOOP
# =====================
def bg_loop():
    while True:
        with lock:
            check_daily_reset()
            for t in timers_real:
                if t["running"]:
                    t["elapsed"] += 1

            n = now()
            if n.minute == 0 and n.second == 0:
                if FIRST_LOG_HOUR <= n.hour <= LAST_LOG_HOUR:
                    log_to_sheet()

            if simulation["enabled"]:
                simulation["now"] += timedelta(seconds=1)

        time.sleep(1)

threading.Thread(target=bg_loop, daemon=True).start()

# =====================
# ROUTES
# =====================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    ts = active_timers()
    return jsonify({
        "now_str": now().strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation["enabled"],
        "timers": [fmt(t["elapsed"]) for t in ts]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    with lock:
        t = active_timers()[i-1]
        if not t["running"]:
            t["running"] = True
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    with lock:
        t = active_timers()[i-1]
        t["running"] = False
    return ("", 204)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    with lock:
        active_timers()[i-1] = new_timer()
    return ("", 204)

@app.route("/api/log-now", methods=["POST"])
def manual_log():
    with lock:
        gs_connect()
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