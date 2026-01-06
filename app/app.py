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
    dt = datetime.fromisoformat(iso)
    return TZ.localize(dt)


def set_sim_now(dt):
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))


def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()


def current_mode():
    return "sim" if sim_enabled() else "real"


# =========================
# GOOGLE CALENDAR
# =========================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_calendar_service():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None

    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=CALENDAR_SCOPES)
    return build("calendar", "v3", credentials=creds)


def update_calendar_daily_summary(calendar_id: str, day: date, activity_time: str) -> bool:
    service = get_calendar_service()
    if service is None:
        return False

    start = TZ.localize(datetime.combine(day, datetime.min.time())) - timedelta(hours=2)
    end = start + timedelta(days=2)   # <<< FIX (הזחה תקינה)

    events = service.events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True
    ).execute().get("items", [])

    for ev in events:
        if ev.get("summary") == "סיכום יום":
            desc = ev.get("description", "") or ""
            lines = desc.splitlines()

            found = False
            for i, line in enumerate(lines):
                if line.startswith("זמן שהיית בפעילות"):
                    lines[i] = f"זמן שהיית בפעילות- {activity_time}"
                    found = True

            if not found:
                lines.append(f"זמן שהיית בפעילות- {activity_time}")

            ev["description"] = "\n".join(lines)

            service.events().update(
                calendarId=calendar_id,
                eventId=ev["id"],
                body=ev
            ).execute()

            return True

    return False


# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run(debug=True)