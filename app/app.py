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

CRON_TOKEN = os.getenv("CRON_TOKEN", "").strip()

DATE_ROW = 3
NAMES_ROW = 5
START_ROW = 7
FIRST_HOUR = 8
LAST_HOUR = 23

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
        raise RuntimeError("GOOGLE_CREDS_JSON missing")

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
        state_ws = sh.add_worksheet(title=STATE_SHEET_NAME, rows=5, cols=5)
        state_ws.update("A1", "state_json")
        state_ws.update("A2", "{}")

    return ws, state_ws

# =========================
# TIME / STATE
# =========================
def now_tz():
    return datetime.now(TZ)

def seconds_to_hms(sec):
    sec = max(0, int(sec))
    return f"{sec//3600:02d}:{(sec%3600)//60:02d}:{sec%60:02d}"

def workday_key(dt):
    cutoff = dt.replace(hour=5, minute=0, second=0, microsecond=0)
    if dt < cutoff:
        dt -= timedelta(days=1)
    return dt.strftime("%d/%m/%Y")

def default_state():
    return {
        "workday": workday_key(now_tz()),
        "last_logged_hour": None,
        "timers": [
            {"running": False, "start": None, "acc": 0}
            for _ in range(TIMER_COUNT)
        ]
    }

def load_state(ws):
    raw = (ws.acell("A2").value or "").strip()
    try:
        return json.loads(raw) if raw else default_state()
    except Exception:
        return default_state()

def save_state(ws, st):
    ws.update("A2", json.dumps(st, ensure_ascii=False))

def current_seconds(timer, now):
    acc = timer["acc"]
    if timer["running"] and timer["start"]:
        acc += int((now - datetime.fromisoformat(timer["start"])).total_seconds())
    return acc

# =========================
# SHEET
# =========================
def row_for_hour(h):
    return START_ROW + (h - FIRST_HOUR)

def ensure_slots(ws):
    labels = []
    for h in range(FIRST_HOUR, LAST_HOUR + 1):
        end = "24:00" if h == 23 else f"{h+1:02d}:00"
        labels.append([f"{h:02d}:00-{end}"])
    ws.update(f"A{START_ROW}:A{START_ROW+len(labels)-1}", labels)

def ensure_date_cols(ws, date):
    row = ws.row_values(DATE_ROW)
    for i, v in enumerate(row, start=1):
        if v == date:
            return i
    col = max(len(row)+1, 2)
    ws.update_cell(DATE_ROW, col, date)
    ws.update_cell(DATE_ROW, col+1, date)
    ws.update_cell(NAMES_ROW, col, "Timer 1")
    ws.update_cell(NAMES_ROW, col+1, "Timer 2")
    return col

# =========================
# CRON LOGIC
# =========================
def cron_tick():
    ws, state_ws = gs_open()
    now = now_tz()
    st = load_state(state_ws)

    if st["workday"] != workday_key(now):
        st = default_state()

    hour = now.hour
    if hour < FIRST_HOUR or hour > LAST_HOUR:
        save_state(state_ws, st)
        return {"skipped": True}

    last = st["last_logged_hour"]
    if last is not None and hour <= last:
        return {"skipped": True}

    ensure_slots(ws)
    base_col = ensure_date_cols(ws, st["workday"])

    for h in range((last or hour), hour+1):
        r = row_for_hour(h)
        for i in range(TIMER_COUNT):
            sec = current_seconds(st["timers"][i], now)
            ws.update_cell(r, base_col+i, seconds_to_hms(sec))
        st["last_logged_hour"] = h

    save_state(state_ws, st)
    return {"ok": True, "hour": hour}

# =========================
# ROUTES
# =========================
@app.route("/")
def home():
    return "Work Timers is running"

@app.route("/api/status")
def status():
    ws, state_ws = gs_open()
    st = load_state(state_ws)
    now = now_tz()
    timers = [
        seconds_to_hms(current_seconds(t, now))
        for t in st["timers"]
    ]
    return jsonify({"workday": st["workday"], "timers": timers})

@app.route("/api/cron/tick")
def cron():
    token = request.args.get("token", "")
    if not CRON_TOKEN or token != CRON_TOKEN:
        return jsonify({"error": "unauthorized"}), 401
    return jsonify(cron_tick())

# 🔍 DEBUG – לבדיקה בלבד
@app.route("/api/debug/env")
def debug_env():
    return jsonify({
        "CRON_TOKEN_env": CRON_TOKEN,
        "token_query": request.args.get("token")
    })

# =========================
# LOCAL
# =========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)