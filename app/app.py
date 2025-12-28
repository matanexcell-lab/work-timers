import os
import json
import time
import threading
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, request

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

def get_sheet_date_and_hour(dt: datetime):
    dt = dt.astimezone(TZ)

    if 0 <= dt.hour < RESET_HOUR:
        sheet_date = (dt - timedelta(days=1)).date()
        sheet_hour = 23
    else:
        sheet_date = dt.date()
        sheet_hour = dt.hour

    return sheet_date, sheet_hour

# =========================
# GOOGLE SHEET WRITE
# =========================
def write_to_sheet(dt):
    global last_logged_hour, current_workday

    sheet_date, sheet_hour = get_sheet_date_and_hour(dt)

    # איפוס יום עבודה ב־05:00
    if current_workday != sheet_date:
        current_workday = sheet_date
        last_logged_hour = None
        for t in timers:
            t["running"] = False
            t["start"] = None
            t["accum"] = 0
        print("🔄 Daily reset")

    if not (FIRST_HOUR <= sheet_hour <= LAST_HOUR):
        return False

    if sheet_hour == last_logged_hour:
        return False

    row = 7 + (sheet_hour - FIRST_HOUR)

    # תאריך בשורה 3
    WS.update_cell(3, 2, sheet_date.strftime("%d/%m/%Y"))
    WS.update_cell(3, 3, sheet_date.strftime("%d/%m/%Y"))

    values = [
        seconds_to_hms(effective_seconds(timers[i], dt))
        for i in range(TIMER_COUNT)
    ]

    WS.update_cell(row, 2, values[0])
    WS.update_cell(row, 3, values[1])

    last_logged_hour = sheet_hour
    print(f"📝 Logged {sheet_hour}:00 → {values}")
    return True

# =========================
# BACKGROUND WORKER
# =========================
def background_worker():
    while True:
        try:
            dt = now()
            if dt.minute == 0:
                write_to_sheet(dt)
        except Exception as e:
            print("❌ Background error:", e)

        time.sleep(30)

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "✅ Work Timers is running"

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "now": dt.strftime("%d/%m/%Y %H:%M:%S"),
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
    ok = write_to_sheet(now())
    return jsonify({"logged": ok})

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    current_workday = get_sheet_date_and_hour(now())[0]
    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)