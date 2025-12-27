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
simulation_enabled = False
simulated_now = None

# =========================
# TIME HELPERS
# =========================
def now():
    if simulation_enabled and simulated_now:
        return simulated_now
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
# GOOGLE SHEET WRITE (⭐ משודרג ⭐)
# =========================
def write_hour(hour):
    date_str = current_workday

    # חיפוש התאריך בשורה 3
    header = WS.row_values(3)
    date_col = None

    for i, val in enumerate(header, start=1):
        if val.strip() == date_str:
            date_col = i
            break

    if not date_col:
        raise RuntimeError(f"❌ Date {date_str} not found in row 3")

    # חישוב שורה לפי שעה
    row = 7 + (hour - FIRST_HOUR)

    values = [
        seconds_to_hms(effective_seconds(timers[i], now()))
        for i in range(TIMER_COUNT)
    ]

    WS.update_cell(row, date_col, values[0])
    WS.update_cell(row, date_col + 1, values[1])

    return {
        "date": date_str,
        "hour": hour,
        "row": row,
        "col_timer1": date_col,
        "col_timer2": date_col + 1,
        "values": values
    }

# =========================
# BACKGROUND WORKER
# =========================
def background_worker():
    global last_logged_hour, current_workday

    while True:
        dt = now()
        wd = workday_key(dt)

        # איפוס יומי
        if current_workday != wd:
            current_workday = wd
            last_logged_hour = None
            for t in timers:
                t["running"] = False
                t["start"] = None
                t["accum"] = 0
            print("🔄 Daily reset")

        # שעה עגולה
        if (
            dt.minute == 0
            and FIRST_HOUR <= dt.hour <= LAST_HOUR
            and dt.hour != last_logged_hour
        ):
            info = write_hour(dt.hour)
            last_logged_hour = dt.hour
            print("📝 Logged:", info)

        time.sleep(30)

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
    dt = now()
    return jsonify({
        "now": dt.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation_enabled,
        "workday": current_workday,
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

    info = write_hour(dt.hour)
    return jsonify({
        "logged": True,
        **info
    })

# =========================
# SIMULATION
# =========================
@app.route("/api/simulate", methods=["POST"])
def simulate():
    global simulation_enabled, simulated_now

    data = request.json
    enabled = data.get("enabled", False)

    if enabled:
        date = data["date"]      # dd/mm/yyyy
        hour = int(data["hour"])
        simulated_now = TZ.localize(
            datetime.strptime(date, "%d/%m/%Y").replace(hour=hour)
        )
        simulation_enabled = True
    else:
        simulation_enabled = False
        simulated_now = None

    return jsonify({
        "simulation": simulation_enabled,
        "now": now().strftime("%d/%m/%Y %H:%M:%S")
    })

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    current_workday = workday_key(now())
    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)