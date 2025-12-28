import os, json, time, threading
from datetime import datetime, timedelta
import pytz

from flask import Flask, jsonify, request, render_template

# =====================
# TIMEZONE
# =====================
TZ = pytz.timezone("Asia/Jerusalem")

def now():
    if SIMULATION["enabled"]:
        return SIMULATION["dt"]
    return datetime.now(TZ)

# =====================
# CONFIG
# =====================
RESET_HOUR = 5
FIRST_HOUR = 8
LAST_HOUR = 23

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# =====================
# FLASK
# =====================
app = Flask(__name__)

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

WS = gs_connect()

# =====================
# STATE
# =====================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(2)
]

CURRENT_WORKDAY = None
START_RECORDED = False
LAST_LOGGED_HOUR = None

SIMULATION = {
    "enabled": False,
    "dt": None
}

# =====================
# HELPERS
# =====================
def seconds_to_hms(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def effective_seconds(t):
    sec = t["accum"]
    if t["running"] and t["start"]:
        sec += int((now() - t["start"]).total_seconds())
    return max(0, sec)

def workday_key(dt):
    cutoff = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

# =====================
# GOOGLE WRITE
# =====================
def log_hour(hour):
    row = 7 + (hour - FIRST_HOUR)
    WS.update_cell(3, 2, CURRENT_WORKDAY)

    WS.update_cell(row, 2, seconds_to_hms(effective_seconds(timers[0])))
    WS.update_cell(row, 3, seconds_to_hms(effective_seconds(timers[1])))

def log_start_time(dt):
    WS.update_cell(4, 2, dt.strftime("%H:%M"))

# =====================
# BACKGROUND WORKER
# =====================
def worker():
    global CURRENT_WORKDAY, START_RECORDED, LAST_LOGGED_HOUR

    while True:
        dt = now()
        wd = workday_key(dt)

        if CURRENT_WORKDAY != wd:
            CURRENT_WORKDAY = wd
            START_RECORDED = False
            LAST_LOGGED_HOUR = None
            for t in timers:
                t["running"] = False
                t["start"] = None
                t["accum"] = 0

        if dt.minute == 0 and FIRST_HOUR <= dt.hour <= LAST_HOUR:
            if LAST_LOGGED_HOUR != dt.hour:
                log_hour(dt.hour)
                LAST_LOGGED_HOUR = dt.hour

        time.sleep(30)

threading.Thread(target=worker, daemon=True).start()

# =====================
# ROUTES
# =====================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/status")
def status():
    return jsonify({
        "now_str": now().strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": SIMULATION["enabled"],
        "timers": [
            seconds_to_hms(effective_seconds(t))
            for t in timers
        ]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    global START_RECORDED

    if not (1 <= i <= len(timers)):
        return jsonify({"error": "bad timer"}), 400

    t = timers[i-1]

    if not t["running"]:
        t["running"] = True
        t["start"] = now()

        if not START_RECORDED:
            log_start_time(t["start"])
            START_RECORDED = True

    return jsonify({"ok": True})

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if not (1 <= i <= len(timers)):
        return jsonify({"error": "bad timer"}), 400

    t = timers[i-1]
    if t["running"]:
        t["accum"] += int((now() - t["start"]).total_seconds())
        t["running"] = False
        t["start"] = None

    return jsonify({"ok": True})

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    if not (1 <= i <= len(timers)):
        return jsonify({"error": "bad timer"}), 400

    timers[i-1] = {"running": False, "start": None, "accum": 0}
    return jsonify({"ok": True})

@app.route("/api/log", methods=["POST"])
def log_manual():
    h = now().hour
    if FIRST_HOUR <= h <= LAST_HOUR:
        log_hour(h)
        return jsonify({"logged": True})
    return jsonify({"error": "outside hours"}), 400

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    data = request.json
    SIMULATION["enabled"] = True
    SIMULATION["dt"] = TZ.localize(
        datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    )
    return jsonify({"simulation": True})

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    SIMULATION["enabled"] = False
    SIMULATION["dt"] = None
    return jsonify({"simulation": False})

# =====================
# RUN
# =====================
if __name__ == "__main__":
    CURRENT_WORKDAY = workday_key(now())
    app.run(host="0.0.0.0", port=5000)