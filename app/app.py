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
    if sec < 0:
        sec = 0
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def workday_key(dt: datetime) -> str:
    # "יום עבודה" מתחלף ב-05:00
    cutoff = dt.replace(hour=5, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt = dt - timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

def default_state():
    return {
        "workday": workday_key(now_tz()),
        "last_logged_hour": None,  # int
        "timers": [
            {"running": False, "start_iso": None, "accumulated": 0}
            for _ in range(TIMER_COUNT)
        ],
    }

def load_state(state_ws):
    raw = (state_ws.acell("A2").value or "").strip()
    if not raw:
        return default_state()
    try:
        st = json.loads(raw)
    except Exception:
        st = default_state()

    # normalize / fill
    if "timers" not in st or not isinstance(st["timers"], list):
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
        # reset everything at 05:00 boundary
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
    # Fill column A with 08:00-09:00 .. 23:00-24:00 if missing
    # Only writes if empty in those rows.
    labels = []
    for h in range(FIRST_HOUR, LAST_HOUR + 1):
        h2 = h + 1
        end = "24:00" if h2 == 24 else f"{h2:02d}:00"
        labels.append(f"{h:02d}:00-{end}")

    # Read existing
    rng = f"A{START_ROW}:A{START_ROW + len(labels) - 1}"
    existing = ws.get(rng)
    existing_vals = [row[0] if row else "" for row in existing]
    updates = []
    for i, label in enumerate(labels):
        if i >= len(existing_vals) or (existing_vals[i] or "").strip() == "":
            updates.append([label])
        else:
            updates.append([existing_vals[i]])

    ws.update(rng, updates)

def find_or_create_date_columns(ws, date_str: str) -> int:
    """
    Returns first column index for the day's timers block (two columns).
    We store date in DATE_ROW across those two columns (same text).
    """
    # Read row
    row_vals = ws.row_values(DATE_ROW)
    # Find first occurrence
    for idx, v in enumerate(row_vals, start=1):
        if (v or "").strip() == date_str:
            return idx

    # Not found -> append at end (ensure at least col A exists)
    start_col = max(2, len(row_vals) + 1)  # avoid column A which is time labels
    # We need two columns: start_col and start_col+1
    ws.update_cell(DATE_ROW, start_col, date_str)
    ws.update_cell(DATE_ROW, start_col + 1, date_str)

    # Names row (under date)
    ws.update_cell(NAMES_ROW, start_col, "Timer 1")
    ws.update_cell(NAMES_ROW, start_col + 1, "Timer 2")
    return start_col

def row_for_hour_slot(hour: int) -> int:
    # hour 8 -> row START_ROW (08:00-09:00)
    return START_ROW + (hour - FIRST_HOUR)

def write_cumulative_to_sheet(ws, date_str: str, hour_slot: int, timer_values_hms):
    """
    Writes HH:MM:SS for each timer into the row of that hour slot.
    """
    ensure_time_slots(ws)
    base_col = find_or_create_date_columns(ws, date_str)
    r = row_for_hour_slot(hour_slot)

    # Two timers -> base_col, base_col+1
    ws.update_cell(r, base_col, timer_values_hms[0])
    ws.update_cell(r, base_col + 1, timer_values_hms[1])

# =========================
# AUTO LOGIC
# =========================
def do_tick(sh, ws, state_ws, st, dt):
    # reset if needed
    maybe_daily_reset(st, dt)

    # Decide if we should log now (full hour), and backfill missed hours
    # We'll log hourslots in [FIRST_HOUR..LAST_HOUR]
    curr_hour = dt.hour
    curr_min = dt.minute

    # Only log after the hour has started exactly? We'll allow tick anytime,
    # but we log up to the last full hour boundary.
    # Example: 10:05 -> treat as 10:00 hour slot.
    target_hour = curr_hour
    if curr_min == 0:
        target_hour = curr_hour
    else:
        target_hour = curr_hour  # still OK; cron runs every X minutes

    # If after midnight (00-04), it's still "yesterday workday" until 05,
    # but we only log between 08-23 anyway.
    if target_hour < FIRST_HOUR or target_hour > LAST_HOUR:
        save_state(state_ws, st)
        return {"logged": False, "reason": "outside hours"}

    last = st.get("last_logged_hour")
    if last is None:
        hours_to_write = [target_hour]
    else:
        if target_hour <= last:
            save_state(state_ws, st)
            return {"logged": False, "reason": "already logged", "last": last, "target": target_hour}
        hours_to_write = list(range(last + 1, target_hour + 1))

    # compute cumulative now for each write hour (we write "as of that moment")
    # We'll approximate by using the current time for all backfilled slots.
    # If you want exact at each hour boundary, we can refine later.
    date_str = st["workday"]
    for h in hours_to_write:
        vals = [seconds_to_hms(current_seconds(st["timers"][i], dt)) for i in range(TIMER_COUNT)]
        write_cumulative_to_sheet(ws, date_str, h, vals)
        st["last_logged_hour"] = h

    save_state(state_ws, st)
    return {"logged": True, "hours": hours_to_write, "workday": date_str}

# =========================
# ROUTES (UI + API)
# =========================
@app.route("/")
def home():
    return render_template("index.html", timer_count=TIMER_COUNT)

@app.route("/api/status")
def api_status():
    sh, ws, state_ws = gs_open()
    dt = now_tz()
    st = load_state(state_ws)
    maybe_daily_reset(st, dt)
    save_state(state_ws, st)

    out = []
    for i in range(TIMER_COUNT):
        sec = current_seconds(st["timers"][i], dt)
        out.append({"running": st["timers"][i]["running"], "seconds": sec, "time": seconds_to_hms(sec)})

    return jsonify({
        "workday": st["workday"],
        "last_logged_hour": st.get("last_logged_hour"),
        "timers": out
    })

@app.route("/api/timer/<int:idx>/start", methods=["POST"])
def api_start(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer index"}), 400

    sh, ws, state_ws = gs_open()
    dt = now_tz()
    st = load_state(state_ws)
    maybe_daily_reset(st, dt)

    t = st["timers"][idx - 1]
    if not t["running"]:
        t["running"] = True
        t["start_iso"] = dt.isoformat()

    save_state(state_ws, st)
    sec = current_seconds(t, dt)
    return jsonify({"status": "started", "timer": idx, "running": True, "time": seconds_to_hms(sec)})

@app.route("/api/timer/<int:idx>/stop", methods=["POST"])
def api_stop(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer index"}), 400

    sh, ws, state_ws = gs_open()
    dt = now_tz()
    st = load_state(state_ws)
    maybe_daily_reset(st, dt)

    t = st["timers"][idx - 1]
    if t["running"] and t["start_iso"]:
        st_dt = parse_iso(t["start_iso"])
        if st_dt:
            t["accumulated"] = int(t.get("accumulated", 0) or 0) + int((dt - st_dt).total_seconds())
    t["running"] = False
    t["start_iso"] = None

    save_state(state_ws, st)
    sec = current_seconds(t, dt)
    return jsonify({"status": "stopped", "timer": idx, "running": False, "time": seconds_to_hms(sec)})

@app.route("/api/timer/<int:idx>/reset", methods=["POST"])
def api_reset(idx):
    if idx < 1 or idx > TIMER_COUNT:
        return jsonify({"error": "invalid timer index"}), 400

    sh, ws, state_ws = gs_open()
    dt = now_tz()
    st = load_state(state_ws)
    maybe_daily_reset(st, dt)

    st["timers"][idx - 1] = {"running": False, "start_iso": None, "accumulated": 0}
    save_state(state_ws, st)
    return jsonify({"status": "reset", "timer": idx})

@app.route("/api/cron/tick", methods=["GET"])
def api_cron_tick():
    # Protect with token
    token = request.args.get("token", "")
    if CRON_TOKEN and token != CRON_TOKEN:
        return jsonify({"error": "unauthorized"}), 401

    sh, ws, state_ws = gs_open()
    dt = now_tz()
    st = load_state(state_ws)
    result = do_tick(sh, ws, state_ws, st, dt)
    return jsonify(result)

# For local dev
if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)