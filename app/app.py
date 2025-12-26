from flask import Flask, jsonify, render_template_string
from datetime import datetime

app = Flask(__name__)

# =========================
# STATE – 2 TIMERS
# =========================
timers = [
    {"running": False, "start_time": None, "accumulated": 0},
    {"running": False, "start_time": None, "accumulated": 0},
]

# =========================
# HELPERS
# =========================
def current_seconds(timer):
    if timer["running"] and timer["start_time"]:
        delta = datetime.now() - timer["start_time"]
        return timer["accumulated"] + int(delta.total_seconds())
    return timer["accumulated"]

def seconds_to_hms(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    html = """
    <h1>⏱ Work Timers</h1>

    {% for i in range(2) %}
      <div style="border:1px solid #ccc; padding:15px; margin:15px; width:300px">
        <h3>Timer {{ i+1 }}</h3>
        <div id="time{{i}}">00:00:00</div>
        <button onclick="action({{i}}, 'start')">Start</button>
        <button onclick="action({{i}}, 'stop')">Stop</button>
        <button onclick="action({{i}}, 'reset')">Reset</button>
      </div>
    {% endfor %}

    <script>
      async function refresh() {
        const r = await fetch('/timers');
        const data = await r.json();
        for (let i=0;i<2;i++) {
          document.getElementById('time'+i).innerText = data['timer_'+(i+1)].time;
        }
      }

      async function action(i, act) {
        await fetch(`/timer/${i+1}/${act}`);
        refresh();
      }

      setInterval(refresh, 1000);
      refresh();
    </script>
    """
    return render_template_string(html)

@app.route("/timers")
def timers_status():
    return jsonify({
        f"timer_{i+1}": {
            "running": timers[i]["running"],
            "seconds": current_seconds(timers[i]),
            "time": seconds_to_hms(current_seconds(timers[i]))
        }
        for i in range(2)
    })

@app.route("/timer/<int:i>/start")
def start_timer(i):
    t = timers[i-1]
    if not t["running"]:
        t["running"] = True
        t["start_time"] = datetime.now()
    return jsonify(ok=True)

@app.route("/timer/<int:i>/stop")
def stop_timer(i):
    t = timers[i-1]
    if t["running"]:
        delta = datetime.now() - t["start_time"]
        t["accumulated"] += int(delta.total_seconds())
    t["running"] = False
    t["start_time"] = None
    return jsonify(ok=True)

@app.route("/timer/<int:i>/reset")
def reset_timer(i):
    timers[i-1] = {"running": False, "start_time": None, "accumulated": 0}
    return jsonify(ok=True)

# =========================
# ENTRY POINT (Render)
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
