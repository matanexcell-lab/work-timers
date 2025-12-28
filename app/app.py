from flask import Flask, render_template, jsonify, request
from datetime import datetime
import time
import os

import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# =====================
# GOOGLE SHEETS CONFIG
# =====================
SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

cred_path = os.path.join(os.path.dirname(__file__), "..", "credentials.json")
creds = Credentials.from_service_account_file(cred_path, scopes=SCOPES)
gc = gspread.authorize(creds)
ws = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

# =====================
# STATE
# =====================
timers = {
    1: {"running": False, "start": None, "elapsed": 0},
    2: {"running": False, "start": None, "elapsed": 0},
}

# =====================
# HELPERS
# =====================
def now_ts():
    return time.time()

def format_seconds(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def timer_total(i):
    t = timers[i]
    total = t["elapsed"]
    if t["running"]:
        total += now_ts() - t["start"]
    return int(total)

# =====================
# UI
# =====================
@app.route("/")
def index():
    return render_template("index.html")

# =====================
# API
# =====================
@app.route("/api/status")
def status():
    return jsonify({
        "now_str": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "timers": [
            format_seconds(timer_total(1)),
            format_seconds(timer_total(2)),
        ]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    t = timers[i]
    if not t["running"]:
        t["running"] = True
        t["start"] = now_ts()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    t = timers[i]
    if t["running"]:
        t["elapsed"] += now_ts() - t["start"]
        t["running"] = False
        t["start"] = None
    return ("", 204)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers[i] = {"running": False, "start": None, "elapsed": 0}
    return ("", 204)

# =====================
# MANUAL GOOGLE SHEET LOG
# =====================
@app.route("/api/log", methods=["POST"])
def log_to_sheet():
    now = datetime.now()
    date_str = now.strftime("%d/%m/%Y")
    time_str = now.strftime("%H:%M:%S")

    t1 = format_seconds(timer_total(1))
    t2 = format_seconds(timer_total(2))

    # מוסיף שורה חדשה בסוף הגיליון
    ws.append_row([date_str, time_str, t1, t2])

    return jsonify({"status": "ok"})

# =====================
# RUN
# =====================
if __name__ == "__main__":
    app.run(debug=True)