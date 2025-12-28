from flask import Flask, render_template, jsonify, request
from datetime import datetime
import time

app = Flask(__name__)

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
def format_seconds(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def now_ts():
    return time.time()

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
    out = []
    for t in timers.values():
        total = t["elapsed"]
        if t["running"]:
            total += now_ts() - t["start"]
        out.append(format_seconds(total))

    return jsonify({
        "now_str": datetime.now().strftime("%d/%m/%Y %H:%M:%S"),
        "timers": out
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
# RUN LOCAL
# =====================
if __name__ == "__main__":
    app.run(debug=True)