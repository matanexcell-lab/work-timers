from flask import Flask, render_template, jsonify, request
from datetime import datetime, timedelta
import time

app = Flask(__name__)

# =====================
# TIMER FACTORY
# =====================
def new_timer():
    return {
        "running": False,
        "elapsed": 0.0,
        "last_tick": None
    }

# =====================
# STATE
# =====================
timers_real = [new_timer(), new_timer()]
timers_sim  = [new_timer(), new_timer()]

simulation = {
    "enabled": False,
    "now": None
}

# =====================
# HELPERS
# =====================
def now():
    if simulation["enabled"]:
        return simulation["now"]
    return datetime.now()

def active_timers():
    return timers_sim if simulation["enabled"] else timers_real

def tick_timer(t, delta):
    if t["running"]:
        t["elapsed"] += delta

def fmt(sec):
    sec = int(sec)
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

# =====================
# UI
# =====================
@app.route("/")
def ui():
    return render_template("index.html")

# =====================
# API
# =====================
@app.route("/api/status")
def status():
    n = now()
    timers = active_timers()

    for t in timers:
        if t["running"]:
            if t["last_tick"] is None:
                t["last_tick"] = n
            else:
                delta = (n - t["last_tick"]).total_seconds()
                tick_timer(t, delta)
                t["last_tick"] = n

    return jsonify({
        "now_str": n.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": simulation["enabled"],
        "timers": [fmt(t["elapsed"]) for t in timers]
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    t = active_timers()[i-1]
    if not t["running"]:
        t["running"] = True
        t["last_tick"] = now()
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    t = active_timers()[i-1]
    t["running"] = False
    t["last_tick"] = None
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    timers = active_timers()
    timers[i-1] = new_timer()
    return jsonify(ok=True)

# =====================
# SIMULATION
# =====================
@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    data = request.json
    simulation["enabled"] = True
    simulation["now"] = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    for t in timers_sim:
        t["last_tick"] = simulation["now"]
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])
def sim_stop():
    simulation["enabled"] = False
    simulation["now"] = None
    return jsonify(ok=True)

# =====================
# RUN
# =====================
if __name__ == "__main__":
    app.run(debug=True)