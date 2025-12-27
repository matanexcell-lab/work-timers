import os, json, time, threading
from datetime import datetime, timedelta
import pytz
from flask import Flask, jsonify, render_template, request

import gspread
from google.oauth2.service_account import Credentials

# ================= CONFIG =================
TZ = pytz.timezone("Asia/Jerusalem")

FIRST_HOUR = 8
LAST_HOUR = 24
RESET_HOUR = 5
TIMER_COUNT = 2

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# ================= FLASK =================
app = Flask(__name__)

# ================= GOOGLE =================
def gs_connect():
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

# ================= TIME / SIM =================
simulation = {
    "enabled": False,
    "dt": None
}

def now():
    return simulation["dt"] if simulation["enabled"] else datetime.now(TZ)

def tick():
    if simulation["enabled"]:
        simulation["dt"] += timedelta(seconds=1)

# ================= STATE =================
timers = [{"running": False, "start": None, "accum": 0} for _ in range(TIMER_COUNT)]
current_workday = None
start_written = False
last_logged_hour = None

# ================= HELPERS =================
def hms(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def effective_seconds(t, dt):
    sec = t["accum"]
    if t["running"]:
        sec += int((dt - t["start"]).total_seconds())
    return sec

def workday_key(dt):
    cutoff = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

# ================= GOOGLE HELPERS =================
def find_or_create_date_col(date_str):
    headers = WS.row_values(3)
    if date_str in headers:
        col = headers.index(date_str) + 1
    else:
        col = len(headers) + 1
        WS.update_cell(3, col, date_str)
        WS.update_cell(3, col + 1, date_str)
    return col

def write_start_time(dt):
    global start_written
    if start_written:
        return
    col = find_or_create_date_col(workday_key(dt))
    WS.update_cell(4, col, dt.strftime("%H:%M"))
    start_written = True

def write_hourly(dt):
    global last_logged_hour
    hour = dt.hour
    if not (FIRST_HOUR <= hour <= LAST_HOUR):
        return
    if hour == last_logged_hour:
        return

    col = find_or_create_date_col(workday_key(dt))
    row = 7 + (hour - FIRST_HOUR)

    for i in range(TIMER_COUNT):
        WS.update_cell(row, col + i, hms(effective_seconds(timers[i], dt)))

    last_logged_hour = hour

# ================= BACKGROUND =================
def background():
    global current_workday, start_written, last_logged_hour
    while True:
        dt = now()
        wd = workday_key(dt)

        if wd != current_workday:
            current_workday = wd
            start_written = False
            last_logged_hour = None
            for t in timers:
                t.update({"running": False, "start": None, "accum": 0})

        if dt.minute == 0:
            write_hourly(dt)

        tick()
        time.sleep(1)

# ================= ROUTES =================
@app.route("/")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "datetime": dt.strftime("%d/%m/%Y %H:%M:%S"),
        "timers": [hms(effective_seconds(t, dt)) for t in timers],
        "simulation": simulation["enabled"]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    t = timers[i - 1]
    if not t["running"]:
        t["running"] = True
        t["start"] = now()
        write_start_time(now())
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    t = timers[i - 1]
    if t["running"]:
        t["accum"] += int((now() - t["start"]).total_seconds())
        t["running"] = False
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers[i - 1] = {"running": False, "start": None, "accum": 0}
    return jsonify(ok=True)

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    date = request.json["date"]
    hour = request.json["hour"]
    simulation["enabled"] = True
    simulation["dt"] = TZ.localize(datetime.strptime(f"{date} {hour}", "%d/%m/%Y %H:%M"))
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    simulation["enabled"] = False
    return jsonify(ok=True)

# ================= MAIN =================
if __name__ == "__main__":
    threading.Thread(target=background, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)