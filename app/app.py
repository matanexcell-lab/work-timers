
from flask import Flask, jsonify, render_template
from datetime import datetime

app = Flask(__name__)

TIMER_COUNT = 2

timers = [
    {"running": False, "start_time": None, "accumulated": 0}
    for _ in range(TIMER_COUNT)
]


def seconds_to_hms(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def current_seconds(timer):
    if timer["running"] and timer["start_time"]:
        delta = datetime.now() - timer["start_time"]
        return timer["accumulated"] + int(delta.total_seconds())
    return timer["accumulated"]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/timers")
def timers_status():
    return jsonify({
        f"timer_{i+1}": seconds_to_hms(current_seconds(timers[i]))
        for i in range(TIMER_COUNT)
    })


@app.route("/timer/<int:i>/start")
def start_timer(i):
    t = timers[i-1]
    if not t["running"]:
        t["running"] = True
        t["start_time"] = datetime.now()
    return "started"


@app.route("/timer/<int:i>/stop")
def stop_timer(i):
    t = timers[i-1]
    if t["running"]:
        t["accumulated"] = current_seconds(t)
        t["running"] = False
        t["start_time"] = None
    return "stopped"


@app.route("/timer/<int:i>/reset")
def reset_timer(i):
    timers[i-1] = {"running": False, "start_time": None, "accumulated": 0}
    return "reset"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
