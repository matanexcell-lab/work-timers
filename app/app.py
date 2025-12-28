import os, json
from datetime import datetime, timedelta
import pytz
from flask import Flask, jsonify, request, render_template

# ================= CONFIG =================
TZ = pytz.timezone("Asia/Jerusalem")
TIMER_COUNT = 2

# ================= GOOGLE SHEETS =================
def gs_connect():
    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(os.environ["GOOGLE_CREDS_JSON"])
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open("Time Tracking")
    return sh.worksheet("Log")

WS = gs_connect()

# ================= STATE =================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(TIMER_COUNT)
]

sim_enabled = False
sim_now = None

# ================= TIME =================
def now():
    if sim_enabled:
        return sim_now
    return datetime.now(TZ)

def fmt(sec):
    return str(timedelta(seconds=int(sec)))

# ================= ROUTES =================
app = Flask(__name__)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    t = now()
    values = []
    for tm in timers:
        total = tm["accum"]
        if tm["running"]:
            total += int((t - tm["start"]).total_seconds())
        values.append(fmt(total))

    return jsonify(
        now_str=t.strftime("%d/%m/%Y %H:%M:%S"),
        simulation=sim_enabled,
        timers=values
    )

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def timer_start(i):
    t = timers[i-1]
    if not t["running"]:
        t["running"] = True
        t["start"] = now()
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def timer_stop(i):
    t = timers[i-1]
    if t["running"]:
        t["accum"] += int((now() - t["start"]).total_seconds())
        t["running"] = False
        t["start"] = None
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def timer_reset(i):
    timers[i-1] = {"running": False, "start": None, "accum": 0}
    return jsonify(ok=True)

# ================= SIMULATION =================
@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    global sim_enabled, sim_now
    data = request.json
    sim_now = TZ.localize(datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M"))
    sim_enabled = True
    return jsonify(ok=True)

@app.route

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    global sim_enabled
    # עצירת כל הטיימרים כדי למנוע קפיצות
    for t in timers:
        t["running"] = False
        t["start"] = None
    sim_enabled = False
    return jsonify(ok=True)

# ================= GOOGLE SHEET =================
@app.route("/api/log", methods=["POST"])
def log_to_sheet():
    t = now()
    col = None
    headers = WS.row_values(3)
    date_str = t.strftime("%d/%m/%Y")

    if date_str in headers:
        col = headers.index(date_str) + 1
    else:
        col = len(headers) + 1
        WS.update_cell(3, col, date_str)

    for i, tm in enumerate(timers):
        WS.update_cell(7+i, col, fmt(tm["accum"]))

    return jsonify(logged=True)

# ================= RUN =================
if __name__ == "__main__":
    app.run()