import os
import json
import sqlite3
from datetime import datetime, timedelta, date

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
    return TZ.localize(datetime.fromisoformat(iso))

def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# RESET
# =========================
def ensure_daily_reset_for_mode(mode, dt):
    if not dt or dt.hour < RESET_HOUR:
        return

    today = dt.date().isoformat()
    key = f"last_reset_date_{mode}"
    if get_meta(key) == today:
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE timers SET running=0, elapsed=0,
        start_epoch=NULL, start_sim_iso=NULL
        WHERE mode=?
    """, (mode,))
    conn.commit()
    conn.close()

    set_meta(key, today)
    set_meta(f"first_start_logged_date_{mode}", "")

# =========================
# TIMERS
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

def timer_total_seconds(mode, i, dt):
    t = timer_row(mode, i)
    if not t:
        return 0

    elapsed = t["elapsed"]
    if not t["running"]:
        return elapsed

    if mode == "real":
        return elapsed + int(tz_now_real().timestamp() - t["start_epoch"])

    start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    return elapsed + int((dt - start).total_seconds())

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

    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
    )
    gc = gspread.authorize(creds)
    WS = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def get_activity_time_for_day(ws, day):
    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return None

    col = headers.index(date_str) + 1
    return ws.cell(22, col).value or "00:00:00"

# =========================
# GOOGLE CALENDAR
# =========================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

def get_calendar_service():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"]
    )
    return build("calendar", "v3", credentials=creds)

def update_calendar_daily_summary(day, activity):
    svc = get_calendar_service()
    if not svc:
        return False

    start = TZ.localize(datetime.combine(day, datetime.min.time()))
    end = start + timedelta(days=1)

    events = svc.events().list(
        calendarId="primary",
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True
    ).execute().get("items", [])

    for ev in events:
        if ev.get("summary") == "סיכום יום":
            ev["description"] = f"זמן שהיית בפעילות- {activity}"
            svc.events().update(
                calendarId="primary",
                eventId=ev["id"],
                body=ev
            ).execute()
            return True

    return False

# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return "OK"

@app.route("/api/debug/daily-summary")
def debug_daily_summary():
    now = tz_now_real()
    yesterday = now.date() - timedelta(days=1)

    ws = gs_connect()
    if not ws:
        return jsonify(ok=False, error="no sheet")

    activity = get_activity_time_for_day(ws, yesterday)

    return jsonify(
        ok=True,
        now=str(now),
        yesterday=str(yesterday),
        activity=activity
    )

@app.route("/api/test-calendar")
def test_calendar():
    yesterday = tz_now_real().date() - timedelta(days=1)
    ws = gs_connect()
    activity = get_activity_time_for_day(ws, yesterday)
    ok = update_calendar_daily_summary(yesterday, activity)
    return jsonify(ok=ok, activity=activity)

@app.route("/api/calendar/update-now", methods=["POST"])
def manual_calendar_update():
    yesterday = tz_now_real().date() - timedelta(days=1)
    ws = gs_connect()
    if not ws:
        return jsonify(ok=False, error="no sheet"), 500

    activity = get_activity_time_for_day(ws, yesterday)
    ok = update_calendar_daily_summary(yesterday, activity)
    return jsonify(ok=ok, activity=activity)

if __name__ == "__main__":
    app.run(debug=True)