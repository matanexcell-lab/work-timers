import os, json, time, threading
from datetime import datetime, timedelta
import pytz
from flask import Flask, jsonify, request, render_template

TZ = pytz.timezone("Asia/Jerusalem")
RESET_HOUR = 5
FIRST_HOUR = 8
LAST_HOUR = 23

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

app = Flask(__name__)

# =====================
# GOOGLE SHEETS
# =====================
def gs_connect():
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds).open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

WS = gs_connect()

# =====================
# STATE
# =====================
def new_timers():
    return [{"running": False, "start": None, "accum": 0} for _ in range(2)]

REAL_TIMERS = new_timers()
SIM_TIMERS = new_timers()

REAL_WORKDAY = None
SIM_WORKDAY = None

REAL_START_RECORDED = False

SIMULATION = {
    "enabled": False,
    "dt": None
}

# =====================
# TIME HELPERS
# =====================
def real_now():
    return datetime.now(TZ)

def sim_now():
    return SIMULATION["dt"]

def now():
    return sim_now() if SIMULATION["enabled"] else real_now()

def active_timers():
    return SIM_TIMERS if SIMULATION["enabled"] else REAL_TIMERS

def seconds_to_hms(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def effective_seconds(t):
    sec = t["accum"]
    if t["running"]:
        sec += int((now() - t["start"]).total_seconds())
    return max(0, sec)

def workday_key(dt):
    cutoff = dt.replace(hour=RESET_HOUR, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

# =====================
# GOOGLE WRITE (REAL ONLY)
# =====================
def log_start_time(dt):
    WS.update_cell(4, 2, dt.strftime("%H:%M"))

def log_hour(hour):
    row = 7 + (hour - FIRST_HOUR)
    WS.update_cell(3, 2, REAL_WORKDAY)
    WS.update_cell(row, 2, seconds_to_hms(effective_seconds(REAL_TIMERS[0])))
    WS.update_cell(row, 3, seconds_to_hms(effective_seconds(REAL_TIMERS[1])))

# =====================
# BACKGROUND RESET
# =====================
def worker():
    global REAL_WORKDAY, SIM_WORKDAY, REAL_START_RECORDED

    while True:
        r_now = real_now()
        s_now = sim_now() if SIMULATION["enabled"] else None

        rk = workday_key(r_now)
        if REAL_WORKDAY != rk:
            REAL_WORKDAY = rk
            REAL_START_RECORDED = False
            for t in REAL_TIMERS:
                t.update({"running": False, "start": None, "accum": 0})

        if SIMULATION["enabled"]:
            sk = workday_key(s_now)
            global SIM_WORKDAY
            if SIM_WORKDAY != sk:
                SIM_WORKDAY = sk
                for t in SIM_TIMERS:
                    t.update({"running": False, "start": None, "accum": 0})

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
        "timers": [seconds_to_hms(effective_seconds(t)) for t in active_timers()]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    global REAL_START_RECORDED

    timers = active_timers()
    t = timers[i-1]

    if not t["running"]:
        t["running"] = True
        t["start"] = now()

        if not SIMULATION["enabled"] and not REAL_START_RECORDED:
            log_start_time(t["start"])
            REAL_START_RECORDED = True

    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    t = active_timers()[i-1]
    if t["running"]:
        t["accum"] += int((now() - t["start"]).total_seconds())
        t["running"] = False
        t["start"] = None
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    active_timers()[i-1] = {"running": False, "start": None, "accum": 0}
    return jsonify(ok=True)

@app.route("/api/log", methods=["POST"])
def log_manual():
    if SIMULATION["enabled"]:
        return jsonify(error="simulation"), 400
    h = real_now().hour
    if FIRST_HOUR <= h <= LAST_HOUR:
        log_hour(h)
        return jsonify(logged=True)
    return jsonify(error="outside hours"), 400

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    dt = datetime.strptime(request.json["datetime"], "%Y-%m-%d %H:%M")
    SIMULATION["enabled"] = True
    SIMULATION["dt"] = TZ.localize(dt)
    return jsonify(simulation=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    SIMULATION["enabled"] = False
    SIMULATION["dt"] = None
    return jsonify(simulation=False)

# =====================
# RUN
# =====================
if __name__ == "__main__":
    REAL_WORKDAY = workday_key(real_now())
    app.run(host="0.0.0.0", port=5000)