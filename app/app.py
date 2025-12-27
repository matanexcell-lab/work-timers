import time
import json
import threading
import os
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify

# =====================
# CONFIG
# =====================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
FIRST_HOUR = 8
LAST_HOUR = 24
RESET_HOUR = 5

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# =====================
# GOOGLE SHEETS
# =====================
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

def safe_get_ws():
    try:
        return gs_connect()
    except Exception as e:
        print("❌ Google Sheets error:", e)
        return None

# =====================
# STATE
# =====================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(TIMER_COUNT)
]

last_logged_hour = None
current_workday = None

# =====================
# HELPERS
# =====================
def now():
    return datetime.now(TZ)

def seconds_to_hms(sec):
    sec = max(0, int(sec))
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
# GOOGLE SHEET WRITE
# =====================
def write_to_sheet(hour, values):
    ws = safe_get_ws()
    if not ws:
        return

    row = 7 + (hour - FIRST_HOUR)

    ws.update_cell(3, 2, current_workday)
    ws.update_cell(3, 3, current_workday)

    ws.update_cell(row, 2, values[0])
    ws.update_cell(row, 3, values[1])

# =====================
# BACKGROUND WORKER
# =====================
def background_worker():
    global current_workday, last_logged_hour

    print("🟢 Background worker started")

    while True:
        try:
            dt = now()
            wd = workday_key(dt)

            # reset daily
            if current_workday != wd:
                current_workday = wd
                last_logged_hour = None
                for t in timers:
                    t["running"] = False
                    t["start"] = None
                    t["accum"] = 0
                print("🔄 Daily reset")

            # full hour logging
            if dt.minute == 0 and FIRST_HOUR <= dt.hour <= LAST_HOUR:
                if dt.hour != last_logged_hour:
                    values = [
                        seconds_to_hms(effective_seconds(t, dt))
                        for t in timers
                    ]
                    write_to_sheet(dt.hour, values)
                    last_logged_hour = dt.hour
                    print(f"📝 Logged hour {dt.hour}")

            time.sleep(30)

        except Exception as e:
            print("❌ Worker error:", e)
            time.sleep(30)

# =====================
# FLASK
# =====================
app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Work Timers is running"

@app.route("/api/timer/<int:i>/start")
def start_timer(i):
    if 1 <= i <= TIMER_COUNT:
        t = timers[i - 1]
        if not t["running"]:
            t["running"] = True
            t["start"] = now()
        return jsonify({"status": "started", "timer": i})
    return jsonify({"error": "invalid timer"}), 400

@app.route("/api/timer/<int:i>/stop")
def stop_timer(i):
    if 1 <= i <= TIMER_COUNT:
        t = timers[i - 1]
        if t["running"]:
            t["accum"] += int((now() - t["start"]).total_seconds())
            t["running"] = False
            t["start"] = None
        return jsonify({"status": "stopped", "timer": i})
    return jsonify({"error": "invalid timer"}), 400

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "workday": current_workday,
        "timers": [
            seconds_to_hms(effective_seconds(t, dt))
            for t in timers
        ]
    })

# =====================
# START BACKGROUND
# =====================
current_workday = workday_key(now())
threading.Thread(target=background_worker, daemon=True).start()