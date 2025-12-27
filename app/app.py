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
RESET_HOUR = 5

# =========================
# APP
# =========================
app = Flask(__name__)

# =========================
# STATE
# =========================
timers = [
    {"running": False, "start": None, "accum": 0}
    for _ in range(TIMER_COUNT)
]

current_workday = None

# סימולציה
simulation_on = False
sim_now = None

# =========================
# TIME HELPERS
# =========================
def real_now():
    return datetime.now(TZ)

def now():
    global sim_now
    if simulation_on and sim_now:
        sim_now += timedelta(seconds=1)
        return sim_now
    return real_now()

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

# =========================
# BACKGROUND RESET
# =========================
def background_worker():
    global current_workday

    while True:
        dt = now()
        wd = workday_key(dt)

        if current_workday != wd:
            current_workday = wd
            for t in timers:
                t["running"] = False
                t["start"] = None
                t["accum"] = 0
            print("🔄 Daily reset")

        time.sleep(1)

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return render_template("index.html")

@app.route("/api/status")
def status():
    dt = now()
    return jsonify({
        "now_str": dt.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation_on,
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
        return jsonify({"ok": True})
    return jsonify({"error": "invalid"}), 400

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if 1 <= i <= TIMER_COUNT:
        t = timers[i - 1]
        if t["running"]:
            t["accum"] += int((now() - t["start"]).total_seconds())
            t["running"] = False
            t["start"] = None
        return jsonify({"ok": True})
    return jsonify({"error": "invalid"}), 400

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    if 1 <= i <= TIMER_COUNT:
        timers[i - 1] = {"running": False, "start": None, "accum": 0}
        return jsonify({"ok": True})
    return jsonify({"error": "invalid"}), 400

# =========================
# SIMULATION
# =========================
@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    global simulation_on, sim_now
    data = request.json
    sim_now = TZ.localize(datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M"))
    simulation_on = True
    return jsonify({"ok": True})

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    global simulation_on, sim_now
    simulation_on = False
    sim_now = None
    return jsonify({"ok": True})

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    current_workday = workday_key(real_now())
    threading.Thread(target=background_worker, daemon=True).start()
    app.run(host="0.0.0.0", port=5000, debug=False)