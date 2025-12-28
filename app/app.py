from flask import Flask, render_template, jsonify, request
from datetime import datetime, timedelta
import time
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =======================
# Google Sheets
# =======================
SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

def get_ws():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(
        "credentials.json", scopes=scopes
    )
    gc = gspread.authorize(creds)
    sh = gc.open(SPREADSHEET_NAME)
    return sh.worksheet(WORKSHEET_NAME)

# =======================
# State
# =======================
timers = [0, 0]
running = [False, False]
last_tick = [None, None]

simulation_on = False
sim_datetime = None

last_workday = None
started_today = False

# =======================
# Time helpers
# =======================
def now():
    return sim_datetime if simulation_on else datetime.now()

def get_workday(dt):
    if dt.hour < 5:
        return (dt - timedelta(days=1)).date()
    return dt.date()

def check_new_day():
    global last_workday, timers, started_today

    wd = get_workday(now())
    if last_workday != wd:
        timers = [0, 0]
        started_today = False
        last_workday = wd

# =======================
# Tick timers
# =======================
def tick():
    for i in range(2):
        if running[i]:
            n = now()
            if last_tick[i]:
                timers[i] += int((n - last_tick[i]).total_seconds())
            last_tick[i] = n

# =======================
# Google Sheet helpers
# =======================
def col_for_date(ws, date_str):
    row = ws.row_values(3)
    for i, v in enumerate(row):
        if v == date_str:
            return i + 1
    raise Exception("Date not found in sheet")

def write_start_time():
    ws = get_ws()
    date_str = get_workday(now()).strftime("%d/%m/%Y")
    col = col_for_date(ws, date_str)
    ws.update_cell(4, col, now().strftime("%H:%M"))

def write_total():
    ws = get_ws()
    date_str = get_workday(now()).strftime("%d/%m/%Y")
    col = col_for_date(ws, date_str)
    total_sec = sum(timers)
    h = total_sec // 3600
    m = (total_sec % 3600) // 60
    ws.update_cell(6, col, f"{h:02}:{m:02}")

# =======================
# Routes
# =======================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    check_new_day()
    tick()
    return jsonify({
        "now_str": now().strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation_on,
        "timers": [time.strftime("%H:%M:%S", time.gmtime(t)) for t in timers]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    global started_today
    check_new_day()
    tick()

    if not started_today:
        write_start_time()
        started_today = True

    running[i-1] = True
    last_tick[i-1] = now()
    return "", 204

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    tick()
    running[i-1] = False
    last_tick[i-1] = None
    return "", 204

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers[i-1] = 0
    running[i-1] = False
    last_tick[i-1] = None
    return "", 204

@app.route("/api/log", methods=["POST"])
def log_sheet():
    tick()
    write_total()
    return jsonify({"logged": True})

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    global simulation_on, sim_datetime
    data = request.json
    sim_datetime = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    simulation_on = True
    return "", 204

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    global simulation_on, sim_datetime
    simulation_on = False
    sim_datetime = None
    return "", 204

# =======================
if __name__ == "__main__":
    app.run(debug=True)