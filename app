from flask import Flask, jsonify, render_template
from datetime import datetime

app = Flask(__name__)

# =========================
# CONFIG
# =========================
TIMER_COUNT = 2

# =========================
# STATE
# =========================
timers = [
    {
        "running": False,
        "start_time": None,
        "accumulated": 0
    }
    for _ in range(TIMER_COUNT)
]

# =========================
# HELPERS
# =========================
def seconds_to_hms(sec: int) -> str:
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def current_seconds(timer):
    if timer["running"] and timer["start_time"]:
        delta = datetime.now() - timer["start_time"]
        return timer["accumulated"] + int(delta.total_seconds())
    return timer["accumulated"]

def timer_status(timer):
    sec = current_seconds(timer)
    return {
        "running": timer["running"],
        "seconds": sec,
        "time": seconds_to_hms(sec)
    }

# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/timers")
def all_timers():
    return jsonify({
        f"timer_{i+1}": timer_status(timers[i])
        for i in range(TIMER_COUNT)
    })

@app.route("/timer/<int:idx>/start")
def start_timer(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer"}), 400
    t = timers[idx - 1]
    if not t["running"]:
        t["running"] = True
        t["start_time"] = datetime.now()
    return jsonify(timer_status(t))

@app.route("/timer/<int:idx>/stop")
def stop_timer(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer"}), 400
    t = timers[idx - 1]
    if t["running"] and t["start_time"]:
        delta = datetime.now() - t["start_time"]
        t["accumulated"] += int(delta.total_seconds())
    t["running"] = False
    t["start_time"] = None
    return jsonify(timer_status(t))

@app.route("/timer/<int:idx>/reset")
def reset_timer(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer"}), 400
    timers[idx - 1] = {
        "running": False,
        "start_time": None,
        "accumulated": 0
    }
    return jsonify(timer_status(timers[idx - 1]))

# =========================
# MAIN
# =========================
if __name__ == "__main__":
    print("🚀 Starting Work Timers Server")
    app.run(host="0.0.0.0", port=5000)
