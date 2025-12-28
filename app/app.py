import os, json, time, threading
from datetime import datetime, timedelta
import pytz
from flask import Flask, jsonify, render_template, request

# ================= CONFIG =================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2
FIRST_HOUR = 8
LAST_HOUR = 23
RESET_HOUR = 5

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# ================= FLASK =================
app = Flask(__name__, template_folder="templates")

# ================= GOOGLE SHEETS =================
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

# ================= STATE =================
timers = [{"running": False, "start": None, "accum": 0} for _ in range(TIMER_COUNT)]
last_logged_hour = None
current_workday = None
start_written = False

# סימולציה
sim_enabled = False
sim_datetime = None

# ================= HELPERS =================
def now():
    return sim_datetime if sim_enabled and sim_datetime else datetime.now(TZ)

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

# ================= GOOGLE WRITE =================
def write_hour(hour, dt):
    global start_written

    date_str = workday_key(dt)

    # חיפוש תאריך בשורה 3 (או יצירה)
    headers = WS.row_values(3)
    if date_str in headers:
        col = headers.index(date_str) + 1
    else:
        col = len(headers) + 1
        WS.update_cell(3, col, date_str)

    # שעת התחלה – פעם אחת ביום
    if not start_written:
        WS.update_cell(4, col, dt.strftime("%H:%M"))
        start_written = True

    row = 7 + (hour - FIRST_HOUR)

    for i in range(TIMER_COUNT):
        WS.update_cell(row, col + i, seconds_to_hms(effective_seconds(timers[i], dt)))

# ================= BACKGROUND =================
def background_worker():
    global current_workday, last_logged_hour, start_written

    while True:
        dt = now()
        wd = workday_key(dt)

        if current_workday != wd:
            current_workday = wd
            last_logged_hour = None
            start_written = False
            for t in timers:
                t.update({"running": False, "start": None, "accum": 0})

        if (
            dt.minute == 0
            and FIRST_HOUR <= dt.hour <= LAST_HOUR
            and dt.hour != last_logged_hour
        ):
            write_hour(dt.hour, dt)
            last_logged_hour = dt.hour

        time.sleep(30)

# ================= ROUTES =================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "now_str": dt.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled,
        "timers": [seconds_to_hms(effective_seconds(timers[i], dt)) for i in range(TIMER_COUNT)]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    dt = now()
    t = timers[i-1]
    if not t["running"]:
        t["running"] = True
        t["start"] = dt
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    dt = now()
    t = timers[i-1]
    if t["running"]:
        t["accum"] += int((dt - t["start"]).total_seconds())
        t["running"] = False
        t["start"] = None
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers[i-1] = {"running": False, "start": None, "accum": 0}
    return jsonify(ok=True)

@app.route("/api/log-now", methods=["POST"])
def log_now():
    dt = now()
    if not (FIRST_HOUR <= dt.hour <= LAST_HOUR):
        return jsonify(ok=False, error="מחוץ לשעות הרישום"), 400
    write_hour(dt.hour, dt)
    return jsonify(ok=True)

@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    global sim_enabled, sim_datetime
    d = request.json["datetime"]
    sim_datetime = TZ.localize(datetime.strptime(d, "%Y-%m-%d %H:%M"))
    sim_enabled = True
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    global sim_enabled
    sim_enabled = False
    return jsonify(ok=True)

# ================= MAIN =================
if __name__ == "__main__":
    current_workday = workday_key(now())
    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)