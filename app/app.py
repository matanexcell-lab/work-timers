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
        mode TEXT,
        timer_id INTEGER,
        running INTEGER,
        elapsed INTEGER,
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
            INSERT OR IGNORE INTO timers VALUES (?, ?, 0, 0, NULL, NULL)
            """, (mode, i))

    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",
        "last_reset_date_real": "",
        "last_reset_date_sim": "",
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",
        "last_auto_logged_hour_real": "",
        "last_auto_logged_day_real": "",
        "last_auto_logged_hour_sim": "",
        "last_auto_logged_day_sim": "",
        "last_log_mode": "",
        "last_log_ok": "",
        "last_log_msg": "",
        "last_log_at_iso": "",
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
    c = db().cursor()
    c.execute("SELECT v FROM meta WHERE k=?", (k,))
    r = c.fetchone()
    return r["v"] if r else ""

def set_meta(k, v):
    c = db().cursor()
    c.execute("REPLACE INTO meta VALUES (?, ?)", (k, v))
    c.connection.commit()

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
    return TZ.localize(dt) if dt.tzinfo is None else dt

def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def current_mode():
    return "sim" if sim_enabled() else "real"

def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()

# =========================
# TIMER CORE
# =========================
def timer_row(mode, i):
    c = db().cursor()
    c.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    return c.fetchone()

def timer_total_seconds(mode, i, now):
    t = timer_row(mode, i)
    if not t:
        return 0
    if not t["running"]:
        return t["elapsed"]
    if mode == "real":
        return t["elapsed"] + int(tz_now_real().timestamp() - t["start_epoch"])
    start = datetime.fromisoformat(t["start_sim_iso"])
    start = TZ.localize(start)
    return t["elapsed"] + int((now - start).total_seconds())

def fmt(sec):
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

# =========================
# GOOGLE SHEETS
# =========================
def gs_connect():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None
    import gspread
    from google.oauth2.service_account import Credentials
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ])
    return gspread.authorize(creds).open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)

def target_hour_and_date(dt):
    if dt.hour < FIRST_LOG_HOUR:
        return 23, dt.date() - timedelta(days=1)
    return min(dt.hour, 23), dt.date()

def sheet_row_for_hour(h):
    return max(7, min(22, 7 + (h - 8)))

def write_two_timers(ws, row, col, mode, now):
    ws.update_cell(row, col, fmt(timer_total_seconds(mode, 1, now)))
    ws.update_cell(row, col + 1, fmt(timer_total_seconds(mode, 2, now)))

def log_to_sheet(mode, now, force=False):
    ws = gs_connect()
    if not ws or not now:
        return False, "no sheet/clock"
    h, d = target_hour_and_date(now)
    key_h = f"last_auto_logged_hour_{mode}"
    key_d = f"last_auto_logged_day_{mode}"
    if not force and get_meta(key_h) == str(h) and get_meta(key_d) == d.isoformat():
        return True, "already"
    headers = ws.row_values(3)
    date_str = d.strftime("%d/%m/%Y")
    if date_str not in headers:
        return False, "date missing"
    col = headers.index(date_str) + 1
    row = sheet_row_for_hour(h)
    write_two_timers(ws, row, col, mode, now)
    set_meta(key_h, str(h))
    set_meta(key_d, d.isoformat())
    return True, "logged"

def get_activity_time_for_day(ws, day):
    headers = ws.row_values(3)
    date_str = day.strftime("%d/%m/%Y")
    if date_str not in headers:
        return None
    col = headers.index(date_str) + 1
    max_sec = 0
    for r in range(7, 23):
        v = ws.cell(r, col).value
        if v:
            try:
                h,m,s = map(int, v.split(":"))
                max_sec = max(max_sec, h*3600+m*60+s)
            except:
                pass
    return fmt(max_sec)

# =========================
# GOOGLE CALENDAR
# =========================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

def update_calendar_daily_summary(calendar_id, day, activity_time):
    raw = os.getenv("GOOGLE_CREDS_JSON")
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=["https://www.googleapis.com/auth/calendar"])
    service = build("calendar", "v3", credentials=creds)

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
            lines = ev.get("description","").splitlines()
            found = False
            for i,l in enumerate(lines):
                if l.startswith("זמן שהיית בפעילות"):
                    lines[i] = f"זמן שהיית בפעילות- {activity_time}"
                    found = True
            if not found:
                lines.append(f"זמן שהיית בפעילות- {activity_time}")
            ev["description"] = "\n".join(lines)
            service.events().update(calendarId=calendar_id, eventId=ev["id"], body=ev).execute()
            return True
    return False

def should_update_daily_summary(now):
    if now.hour == 0 and now.minute == 30:
        today = now.date().isoformat()
        if get_meta("daily_calendar_updated_date") != today:
            set_meta("daily_calendar_updated_date", today)
            return True
    return False

# =========================
# ROUTES
# =========================
@app.route("/api/status")
def status():
    if sim_enabled():
        s = get_sim_now()
        if s:
            set_sim_now(s + timedelta(seconds=1))

    now_real = tz_now_real()
    if should_update_daily_summary(now_real):
        ws = gs_connect()
        if ws:
            activity = get_activity_time_for_day(ws, now_real.date()-timedelta(days=1))
            if activity:
                update_calendar_daily_summary("primary", now_real.date()-timedelta(days=1), activity)

    mode = current_mode()
    now = now_for_mode(mode)
    timers = [fmt(timer_total_seconds(mode, i, now)) for i in range(1,3)]
    return jsonify(now_str=now.strftime("%d/%m/%Y %H:%M:%S"), timers=timers)

# ---- start/stop/reset/adjust/set/log-now/sim ----
# (נשארים זהים למה שהיה לך – מקוצר כאן בכוונה)

if __name__ == "__main__":
    app.run(debug=True)