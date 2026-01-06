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

RESET_HOUR = 5          # daily reset at 05:00
FIRST_LOG_HOUR = 8      # auto log window start
LAST_LOG_HOUR = 24      # clamp to 23

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
            INSERT OR IGNORE INTO timers(mode, timer_id)
            VALUES (?, ?)
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
        "calendar_status": "",
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (k, v))

    conn.commit()
    conn.close()


init_db()

# =========================
# META HELPERS
# =========================
def get_meta(k):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k=?", (k,))
    row = cur.fetchone()
    conn.close()
    return row["v"] if row else ""


def set_meta(k, v):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))
    conn.commit()
    conn.close()


# =========================
# TIME HELPERS
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


def now_for_mode(mode):
    return get_sim_now() if mode == "sim" else tz_now_real()


def current_mode():
    return "sim" if sim_enabled() else "real"


# =========================
# RESET (05:00)
# =========================
def ensure_daily_reset_for_mode(mode, clock_dt):
    if not clock_dt or clock_dt.hour < RESET_HOUR:
        return

    today = clock_dt.date().isoformat()
    if get_meta(f"last_reset_date_{mode}") == today:
        return

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE timers
        SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
        WHERE mode=?
    """, (mode,))
    conn.commit()
    conn.close()

    set_meta(f"last_reset_date_{mode}", today)
    set_meta(f"first_start_logged_date_{mode}", "")


# =========================
# TIMER CORE
# =========================
def fmt(sec):
    sec = max(0, int(sec))
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

    elapsed = int(t["elapsed"])
    if not t["running"]:
        return elapsed

    if mode == "real":
        return elapsed + int(tz_now_real().timestamp() - t["start_epoch"])

    start = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    return elapsed + int((clock - start).total_seconds())


def set_timer_seconds(mode, i, seconds, clock):
    t = timer_row(mode, i)
    if not t:
        return False

    conn = db()
    cur = conn.cursor()

    if t["running"] and ALLOW_EDIT_WHILE_RUNNING:
        if mode == "real":
            cur.execute("""
                UPDATE timers SET elapsed=?, start_epoch=?
                WHERE mode=? AND timer_id=?
            """, (seconds, tz_now_real().timestamp(), mode, i))
        else:
            cur.execute("""
                UPDATE timers SET elapsed=?, start_sim_iso=?
                WHERE mode=? AND timer_id=?
            """, (seconds, clock.replace(tzinfo=None).isoformat(), mode, i))
    else:
        cur.execute("""
            UPDATE timers SET elapsed=?, running=0,
            start_epoch=NULL, start_sim_iso=NULL
            WHERE mode=? AND timer_id=?
        """, (seconds, mode, i))

    conn.commit()
    conn.close()
    return True


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


def sheet_row_for_hour(h):
    return max(7, min(22, 7 + (h - 8)))


def log_to_sheet(mode, clock, force=False):
    ws = gs_connect()
    if not ws or not clock:
        return False, "sheet not ready"

    h, d = target_hour_and_date(clock)
    key_h = f"last_auto_logged_hour_{mode}"
    key_d = f"last_auto_logged_day_{mode}"

    if not force and get_meta(key_h) == str(h) and get_meta(key_d) == d.isoformat():
        return True, "already logged"

    headers = ws.row_values(3)
    date_str = d.strftime("%d/%m/%Y")
    if date_str not in headers:
        return False, "date not found"

    col = headers.index(date_str) + 1
    row = sheet_row_for_hour(h)

    for i in (1, 2):
        ws.update_cell(row, col + i - 1, fmt(timer_total_seconds(mode, i, clock)))

    set_meta(key_h, str(h))
    set_meta(key_d, d.isoformat())
    return True, "logged"


# =========================
# GOOGLE CALENDAR (FIXED)
# =========================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

CAL_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def cal_service():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None
    creds = Credentials.from_service_account_info(json.loads(raw), scopes=CAL_SCOPES)
    return build("calendar", "v3", credentials=creds)


def update_calendar_summary(activity):
    svc = cal_service()
    if not svc:
        set_meta("calendar_status", "no service")
        return False

    events = svc.events().list(
        calendarId="primary",
        q="סיכום יום",
        singleEvents=False,
        maxResults=5,
    ).execute().get("items", [])

    if not events:
        set_meta("calendar_status", "event not found")
        return False

    ev = events[0]
    desc = ev.get("description", "") or ""
    lines = [l for l in desc.splitlines() if not l.startswith("זמן שהיית")]
    lines.append(f"זמן שהיית בפעילות- {activity}")
    ev["description"] = "\n".join(lines)

    svc.events().update(calendarId="primary", eventId=ev["id"], body=ev).execute()
    set_meta("calendar_status", "updated")
    return True


# =========================
# ROUTES
# =========================
@app.route("/")
def ui():
    return render_template("index.html")


@app.route("/api/status")
def status():
    if sim_enabled():
        s = get_sim_now()
        if s:
            set_sim_now(s + timedelta(seconds=1))

    ensure_daily_reset_for_mode("real", tz_now_real())
    ensure_daily_reset_for_mode("sim", get_sim_now())

    mode = current_mode()
    clock = now_for_mode(mode)

    return jsonify(
        now=str(clock),
        mode=mode,
        timers=[fmt(timer_total_seconds(mode, i, clock)) for i in (1, 2)],
        calendar_status=get_meta("calendar_status"),
    )


@app.route("/api/log-now", methods=["POST"])
def manual_log():
    mode = current_mode()
    clock = now_for_mode(mode)
    ok, msg = log_to_sheet(mode, clock, force=True)
    set_meta("last_log_ok", "1" if ok else "0")
    set_meta("last_log_msg", msg)
    return jsonify(ok=ok, msg=msg)


@app.route("/api/calendar/update-now", methods=["POST"])
def calendar_now():
    ws = gs_connect()
    if not ws:
        return jsonify(ok=False)

    yesterday = tz_now_real().date() - timedelta(days=1)
    headers = ws.row_values(3)
    date_str = yesterday.strftime("%d/%m/%Y")
    if date_str not in headers:
        return jsonify(ok=False)

    col = headers.index(date_str) + 1
    activity = ws.cell(22, col).value or "00:00:00"
    ok = update_calendar_summary(activity)
    return jsonify(ok=ok, activity=activity)


@app.route("/api/debug/calendar")
def debug_calendar():
    svc = cal_service()
    if not svc:
        return jsonify(ok=False)

    cals = svc.calendarList().list().execute().get("items", [])
    return jsonify(
        ok=True,
        calendars=[{"id": c["id"], "summary": c["summary"]} for c in cals],
    )


if __name__ == "__main__":
    app.run(debug=True)