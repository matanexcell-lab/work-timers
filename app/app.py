import os, json, time, threading
from datetime import datetime, timedelta
import pytz

from flask import Flask, jsonify, request, render_template

# =====================
# CONFIG
# =====================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
RESET_HOUR = 5
FIRST_HOUR = 8
LAST_HOUR = 23

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# =====================
# FLASK
# =====================
app = Flask(__name__, template_folder="../templates")

# =====================
# SIMULATION STATE
# =====================
simulation_enabled = False
simulated_now = None

# =====================
# TIMERS STATE
# =====================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(TIMER_COUNT)
]

current_workday = None
last_logged_hour = None
start_logged = False

# =====================
# TIME HELPERS
# =====================
def now():
    global simulated_now
    if simulation_enabled and simulated_now:
        simulated_now += timedelta(seconds=1)
        return simulated_now
    return datetime.now(TZ)

def seconds_to_hms(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def effective_seconds(t, dt):
    sec = t["accum"]
    if t["running"] and t["start"]:
        sec += int((dt - t["start"]).total_seconds())
    return sec

def workday_key(dt):
    cutoff = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

# =====================
# GOOGLE SHEETS
# =====================
def gs_connect():
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.getenv("GOOGLE_CREDS_JSON")
    info = json.loads(raw)

    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    gc = gspread.authorize(creds)
    return gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

WS = gs_connect()

def write_hour(hour, dt):
    date_str = workday_key(dt)

    # חיפוש עמוד לפי תאריך בשורה 3
    headers = WS.row_values(3)
    if date_str in headers:
        col = headers.index(date_str) + 1
    else:
        col = len(headers) + 1
        WS.update_cell(3, col, date_str)

    row = 7 + (hour - FIRST_HOUR)

    values = [
        seconds_to_hms(effective_seconds(timers[i], dt))
        for i in range(TIMER_COUNT)
    ]

    for i, v in enumerate(values):
        WS.update_cell(row, col + i, v)

# =====================
# BACKGROUND WORKER
# =====================
def worker():
    global current_workday, last_logged_hour, start_logged

    while True:
        dt = now()
        wd = workday_key(dt)

        if wd != current_workday:
            current_workday = wd
            last_logged_hour = None
            start_logged = False
            for t in timers:
                t.update({"running": False, "start": None, "accum": 0})

        if dt.minute == 0 and FIRST_HOUR <= dt.hour <= LAST_HOUR:
            if dt.hour != last_logged_hour:
                write_hour(dt.hour, dt)
                last_logged_hour = dt.hour

        time.sleep(1)

# =====================
# ROUTES
# =====================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "now_str": dt.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation_enabled,
        "timers": [
            seconds_to_hms(effective_seconds(timers[i], dt))
            for i in range(TIMER_COUNT)
        ]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    t = timers[i-1]
    if not t["running"]:
        t["running"] = True
        t["start"] = now()
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    t = timers[i-1]
    if t["running"]:
        t["accum"] += int((now() - t["start"]).total_seconds())
        t["running"] = False
        t["start"] = None
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers[i-1] = {"running": False, "start": None, "accum": 0}
    return jsonify(ok=True)

# ===== SIMULATION =====
@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    global simulation_enabled, simulated_now
    data = request.json
    simulated_now = TZ.localize(
        datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    )
    simulation_enabled = True
    return jsonify(simulation=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    global simulation_enabled, simulated_now
    simulation_enabled = False
    simulated_now = None
    return jsonify(simulation=False)

# =====================
# MAIN
# =====================
if __name__ == "__main__":
    threading.Thread(target=worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)