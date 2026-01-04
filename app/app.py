import os
import json
import sqlite3
from datetime import datetime, timedelta

import pytz
from flask import Flask, jsonify, render_template, request

# =========================
# APP
# =========================
app = Flask(__name__, template_folder="templates")
TZ = pytz.timezone("Asia/Jerusalem")

# =========================
# CONFIG
# =========================
TIMER_COUNT = 2
RESET_HOUR = 5
FIRST_LOG_HOUR = 8
LAST_LOG_HOUR = 24

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

ALLOW_EDIT_WHILE_RUNNING = True

# =========================
# SQLITE
# =========================
DB_PATH = os.getenv("DB_PATH", "/tmp/work_timers.db")

def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS timers (
        mode TEXT NOT NULL,
        timer_id INTEGER NOT NULL,
        running INTEGER NOT NULL DEFAULT 0,
        elapsed INTEGER NOT NULL DEFAULT 0,
        start_epoch REAL,
        start_sim_iso TEXT,
        PRIMARY KEY (mode, timer_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)

    for mode in ("real", "sim"):
        for i in range(1, TIMER_COUNT + 1):
            cur.execute("""
            INSERT OR IGNORE INTO timers
            VALUES (?, ?, 0, 0, NULL, NULL)
            """, (mode, i))

    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",
        "last_log_mode": "",
        "last_log_ok": "",
        "last_log_msg": "",
        "last_log_at_iso": "",
        "last_auto_logged_hour_real": "",
        "last_auto_logged_day_real": "",
        "last_auto_logged_hour_sim": "",
        "last_auto_logged_day_sim": "",
        "last_reset_date_real": "",
        "last_reset_date_sim": "",
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",
        "daily_calendar_updated_date": "",
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()

init_db()

# =========================
# META
# =========================
def get_meta(k):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k=?", (k,))
    r = cur.fetchone()
    conn.close()
    return r["v"] if r else ""

def set_meta(k, v):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))
    conn.commit()
    conn.close()

# =========================
# TIME
# =========================
def tz_now_real():
    return datetime.now(TZ)

def sim_enabled():
    return get_meta("sim_enabled") == "1"

def get_sim_now():
    iso = get_meta("sim_now_iso")
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    return TZ.localize(dt)

def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# TIMER
# =========================
def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

def timer_row(mode, i):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    r = cur.fetchone()
    conn.close()
    return r

def timer_total_seconds(mode, i, clock):
    t = timer_row(mode, i)
    if not t:
        return 0

    if not t["running"]:
        return t["elapsed"]

    if mode == "real":
        return t["elapsed"] + int(tz_now_real().timestamp() - t["start_epoch"])

    start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    return t["elapsed"] + int((clock - start).total_seconds())

# =========================
# GOOGLE SHEETS
# =========================
WS = None

def gs_connect():
    global WS
    if WS:
        return WS

    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    creds = Credentials.from_service_account_info(
        json.loads(raw),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    WS = gspread.authorize(creds).open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def target_hour_and_date(dt):
    if dt.hour < FIRST_LOG_HOUR:
        return 23, dt.date() - timedelta(days=1)
    return min(dt.hour, 23), dt.date()

def sheet_row_for_hour(hour):
    return max(7, min(22, 7 + hour - 8))

def write_two_timers_into_sheet(ws, row, col, mode, clock):
    ws.update_cell(row, col, fmt(timer_total_seconds(mode, 1, clock)))
    ws.update_cell(row, col + 1, fmt(timer_total_seconds(mode, 2, clock)))

def log_to_sheet(mode, clock, force=False):
    ws = gs_connect()
    if not ws or not clock:
        return False, "no sheet/clock"

    hour, day = target_hour_and_date(clock)
    date_str = day.strftime("%d/%m/%Y")

    headers = ws.row_values(3)
    if date_str not in headers:
        return False, "date not found"

    col = headers.index(date_str) + 1
    row = sheet_row_for_hour(hour)

    write_two_timers_into_sheet(ws, row, col, mode, clock)
    return True, "logged"

def get_activity_time_for_day(ws, day):
    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return None

    col = headers.index(date_str) + 1
    max_sec = 0

    for row in range(7, 23):
        val = ws.cell(row, col).value
        if val:
            h, m, s = map(int, val.split(":"))
            max_sec = max(max_sec, h*3600 + m*60 + s)

    return fmt(max_sec)

# =========================
# GOOGLE CALENDAR
# =========================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

def get_calendar_service():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None
    return build(
        "calendar",
        "v3",
        credentials=Credentials.from_service_account_info(
            json.loads(raw),
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
    )

def update_calendar_daily_summary(calendar_id, day, activity):
    service = get_calendar_service()
    if not service:
        return False

    start = TZ.localize(datetime.combine(day, datetime.min.time()))
    end = start + timedelta(days=1)

    events = service.events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True
    ).execute().get("items", [])

    for ev in events:
        if ev.get("summary") == "סיכום יום":
            lines = ev.get("description", "").splitlines()
            lines = [l for l in lines if not l.startswith("זמן שהיית בפעילות")]
            lines.append(f"זמן שהיית בפעילות- {activity}")
            ev["description"] = "\n".join(lines)

            service.events().update(
                calendarId=calendar_id,
                eventId=ev["id"],
                body=ev
            ).execute()
            return True
    return False

# =========================
# ROUTES
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    return jsonify(ok=True)

@app.route("/api/log-now", methods=["POST"])
def manual_log():
    mode = current_mode()
    clock = now_for_mode(mode)

    ok, msg = log_to_sheet(mode, clock, force=True)

    set_meta("last_log_mode", mode)
    set_meta("last_log_ok", "1" if ok else "0")
    set_meta("last_log_msg", msg)
    set_meta("last_log_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    return jsonify(ok=ok, message=msg)

@app.route("/api/debug/daily-summary")
def debug_daily_summary():
    ws = gs_connect()
    yesterday = tz_now_real().date() - timedelta(days=1)
    return jsonify(activity=get_activity_time_for_day(ws, yesterday))

if __name__ == "__main__":
    app.run(debug=True)