import os
import json
from datetime import datetime, timedelta
import pytz

from flask import Flask, jsonify, render_template, request

# =========================
# CONFIG
# =========================
TZ = pytz.timezone("Asia/Jerusalem")

TIMER_COUNT = 2

SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Time Tracking")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Log")
STATE_SHEET_NAME = os.getenv("STATE_SHEET_NAME", "_state")

CRON_TOKEN = os.getenv("CRON_TOKEN", "")

DATE_ROW = 3
NAMES_ROW = 5
TIME_COL = 1          # Column A
START_ROW = 7         # Row where time slots begin
FIRST_HOUR = 8
LAST_HOUR = 23        # logs for 23:00-24:00 at 23:00

# =========================
# APP
# =========================
app = Flask(__name__, template_folder="templates")

# =========================
# GOOGLE SHEETS
# =========================
def gs_client():
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.getenv("GOOGLE_CREDS_JSON", "").strip()
    if not raw:
        raise RuntimeError("Missing GOOGLE_CREDS_JSON env var")

    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)

def gs_open():
    gc = gs_client()
    sh = gc.open(SPREADSHEET_NAME)
    ws = sh.worksheet(WORKSHEET_NAME)
    try:
        state_ws = sh.worksheet(STATE_SHEET_NAME)
    except Exception:
        state_ws = sh.add_worksheet(title=STATE_SHEET_NAME, rows=10, cols=5)
        state_ws.update("A1", "state_json")
        state_ws.update("A2", "{}")
    return sh, ws, state_ws

# =========================
# TIME / STATE
# =========================
def now_tz() -> datetime:
    return datetime.now(TZ)

def seconds_to_hms(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def workday_key(dt: datetime) -> str:
    cutoff = dt.replace(hour=5, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

def default_state():
    return {
        "workday": workday_key(now_tz()),
        "last_logged_hour": None,
        "timers": [
            {"running": False, "start_iso": None, "accumulated": 0}
            for _ in range(TIMER_COUNT)
        ],
    }

def load_state(state_ws):
    raw = (state_ws.acell("A2").value or "").strip()
    try:
        st = json.loads(raw) if raw else default_state()
    except Exception:
        st = default_state()

    while len(st["timers"]) < TIMER_COUNT:
        st["timers"].append({"running": False, "start_iso": None, "accumulated": 0})
    st["timers"] = st["timers"][:TIMER_COUNT]

    if "workday" not in st:
        st["workday"] = workday_key(now_tz())

    return st

def save_state(state_ws, st):
    state_ws.update("A2", json.dumps(st, ensure_ascii=False))

def parse_iso(s: str):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = TZ.localize(dt)
        return dt.astimezone(TZ)
    except Exception:
        return None

def current_seconds(timer: dict, dt: datetime) -> int:
    acc = int(timer.get("accumulated", 0) or 0)
    if timer.get("running") and timer.get("start_iso"):
        st = parse_iso(timer["start_iso"])
        if st:
            acc += int((dt - st).total_seconds())
    return acc

def maybe_daily_reset(st: dict, dt: datetime) -> bool:
    wd = workday_key(dt)
    if st.get("workday") != wd:
        st["workday"] = wd
        st["last_logged_hour"] = None
        st["timers"] = [
            {"running": False, "start_iso": None, "accumulated": 0}
            for _ in range(TIMER_COUNT)
        ]
        return True
    return False

# =========================
# SHEET HELPERS
# =========================
def ensure_time_slots(ws):
    labels = []
    for h in range(FIRST_HOUR, LAST_HOUR + 1):
        end = "24:00" if h + 1 == 24 else f"{h+1:02d}:00"
        labels.append(f"{h:02d}:00-{end}")

    rng = f"A{START_ROW}:A{START_ROW + len(labels) - 1}"
    existing = ws.get(rng)
    existing_vals = [row[0] if row else "" for row in existing]

    updates = []
    for i, label in enumerate(labels):
        updates.append([existing_vals[i] if existing_vals[i].strip() else label])

    ws.update(rng, updates)

def find_or_create_date_columns(ws, date_str: str) -> int:
    row_vals = ws.row_values(DATE_ROW)
    for idx, v in enumerate(row_vals, start=1):
        if (v or "").strip() == date_str:
            return idx

    start_col = max(2, len(row_vals) + 1)
    ws.update_cell(DATE_ROW, start_col, date_str)
    ws.update_cell(DATE_ROW, start_col + 1, date_str)
    ws.update_cell(NAMES_ROW, start_col, "Timer 1")
    ws.update_cell(NAMES_ROW, start_col + 1, "Timer 2")
    return start_col

def row_for_hour_slot(hour: int) -> int:
    return START_ROW + (hour - FIRST_HOUR)

def write_cumulative_to_sheet(ws, date_str: str, hour_slot: int, timer_values_hms):
    ensure_time_slots(ws)
    base_col = find_or_create_date_columns(ws, date_str)
    r = row_for_hour_slot(hour_slot)
    ws.update_cell(r, base_col, timer_values_hms[0])
    ws.update_cell(r, base_col + 1, timer_values_hms[1])

# =========================
# AUTO LOGIC
# =========================
def do_tick(sh, ws, state_ws, st, dt):
    maybe_daily_reset(st, dt)

    if dt.hour < FIRST_HOUR or dt.hour > LAST_HOUR:
        save_state(state_ws, st)
        return {"logged": False, "reason": "outside hours"}

    target_hour = dt.hour
    last = st.get("last_logged_hour")

    if last is not None and target_hour <= last:
        return {"logged": False, "reason": "already logged"}

    hours_to_write = [target_hour] if last is None else list(range(last + 1, target_hour + 1))

    for h in hours_to_write:
        vals = [seconds_to_hms(current_seconds(st["timers"][i], dt)) for i in range(TIMER_COUNT)]
        write_cumulative_to_sheet(ws, st["workday"], h, vals)
        st["last_logged_hour"] = h

    save_state(state_ws, st)
    return {"logged": True, "hours": hours_to_write}

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return render_template("index.html", timer_count=TIMER_COUNT)

@app.route("/api/status")
def api_status():
    try:
        sh, ws, state_ws = gs_open()
        dt = now_tz()
        st = load_state(state_ws)
        maybe_daily_reset(st, dt)
        save_state(state_ws, st)

        timers = []
        for i in range(TIMER_COUNT):
            sec = current_seconds(st["timers"][i], dt)
            timers.append({
                "running": st["timers"][i]["running"],
                "seconds": sec,
                "time": seconds_to_hms(sec)
            })

        return jsonify({
            "ok": True,
            "gs_ready": True,
            "workday": st["workday"],
            "last_logged_hour": st.get("last_logged_hour"),
            "timers": timers
        })

    except Exception as e:
        return jsonify({
            "ok": False,
            "gs_ready": False,
            "error": str(e)
        }), 500

@app.route("/api/timer/<int:idx>/start", methods=["POST"])
def api_start(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer"}), 400

    sh, ws, state_ws = gs_open()
    dt = now_tz()
    st = load_state(state_ws)
    maybe_daily_reset(st, dt)

    t = st["timers"][idx - 1]
    if not t["running"]:
        t["running"] = True
        t["start_iso"] = dt.isoformat()

    save_state(state_ws, st)
    return jsonify({"status": "started"})

@app.route("/api/timer/<int:idx>/stop", methods=["POST"])
def api_stop(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer"}), 400

    sh, ws, state_ws = gs_open()
    dt = now_tz()
    st = load_state(state_ws)
    maybe_daily_reset(st, dt)

    t = st["timers"][idx - 1]
    if t["running"] and t["start_iso"]:
        st_dt = parse_iso(t["start_iso"])
        if st_dt:
            t["accumulated"] += int((dt - st_dt).total_seconds())
    t["running"] = False
    t["start_iso"] = None

    save_state(state_ws, st)
    return jsonify({"status": "stopped"})

@app.route("/api/cron/tick")
def api_cron_tick():
    token = request.args.get("token", "")
    if CRON_TOKEN and token != CRON_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    sh, ws, state_ws = gs_open()
    st = load_state(state_ws)
    return jsonify(do_tick(sh, ws, state_ws, st, now_tz()))

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)