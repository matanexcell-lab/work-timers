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
LAST_LOG_HOUR = 24  # "24" maps to last row (23)

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# Edit behavior:
# True  -> allow Set/+5/-10 while running
# False -> block edits while running
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
        mode TEXT NOT NULL,                 -- 'real' or 'sim'
        timer_id INTEGER NOT NULL,
        running INTEGER NOT NULL DEFAULT 0, -- 0/1
        elapsed INTEGER NOT NULL DEFAULT 0, -- base seconds (when not running)
        start_epoch REAL,                   -- unix epoch when started (real)
        start_sim_iso TEXT,                 -- ISO local datetime when started (sim)
        PRIMARY KEY (mode, timer_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """)

    # Ensure timers exist
    for mode in ("real", "sim"):
        for i in range(1, TIMER_COUNT + 1):
            cur.execute("""
            INSERT OR IGNORE INTO timers(mode, timer_id, running, elapsed, start_epoch, start_sim_iso)
            VALUES (?, ?, 0, 0, NULL, NULL)
            """, (mode, i))

    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",

        # last manual log result (for UI)
        "last_log_mode": "",
        "last_log_ok": "",
        "last_log_msg": "",
        "last_log_at_iso": "",

        # AUTO log guards (per mode)
        "last_auto_logged_hour_real": "",
        "last_auto_logged_day_real": "",
        "last_auto_logged_hour_sim": "",
        "last_auto_logged_day_sim": "",

        # resets (per mode)
        "last_reset_date_real": "",
        "last_reset_date_sim": "",

        # start-time written (per mode) - day key YYYY-MM-DD
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (k, v))

    conn.commit()
    conn.close()

init_db()

# =========================
# META HELPERS
# =========================
def get_meta(k: str) -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT v FROM meta WHERE k=?", (k,))
    row = cur.fetchone()
    conn.close()
    return row["v"] if row else ""

def set_meta(k: str, v: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", (k, v))
    conn.commit()
    conn.close()




# =========================
# TIME HELPERS
# =========================
def should_update_daily_summary(now_dt):
    """
    מחזיר True רק פעם אחת ביום – ב־00:30
    """
    if now_dt.hour == 0 and now_dt.minute == 30:
        last = get_meta("daily_calendar_updated_date")  # YYYY-MM-DD
        today = now_dt.date().isoformat()
        if last != today:
            set_meta("daily_calendar_updated_date", today)
            return True
    return False


def tz_now_real():
    return datetime.now(TZ)

def sim_enabled() -> bool:
    return get_meta("sim_enabled") == "1"

def get_sim_now():
    iso = get_meta("sim_now_iso")
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = TZ.localize(dt)
    return dt.astimezone(TZ)

def set_sim_now(dt: datetime):
    dt = dt.astimezone(TZ)
    # store without tzinfo (wall-clock), seconds precision
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now_for_mode(mode: str):
    if mode == "sim":
        return get_sim_now()
    return tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# RESET LOGIC (05:00) - PER MODE
# =========================
def ensure_daily_reset_for_mode(mode: str, clock_dt: datetime):
    if clock_dt is None:
        return
    if clock_dt.hour < RESET_HOUR:
        return

    key_last = f"last_reset_date_{mode}"          # YYYY-MM-DD
    key_start = f"first_start_logged_date_{mode}" # YYYY-MM-DD

    today = clock_dt.date().isoformat()
    if get_meta(key_last) == today:
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

    set_meta(key_last, today)
    set_meta(key_start, "")  # allow start-time again

# =========================
# TIMER CALCULATION
# =========================
def fmt(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

def timer_row(mode: str, timer_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, timer_id))
    t = cur.fetchone()
    conn.close()
    return t

def timer_total_seconds(mode: str, timer_id: int, clock_dt: datetime) -> int:
    t = timer_row(mode, timer_id)
    if t is None:
        return 0

    elapsed = int(t["elapsed"])
    running = int(t["running"]) == 1

    if not running:
        return elapsed

    if mode == "real":
        if t["start_epoch"] is None:
            return elapsed
        now_epoch = tz_now_real().timestamp()
        return elapsed + int(max(0, now_epoch - float(t["start_epoch"])))

    # sim
    if t["start_sim_iso"] is None or clock_dt is None:
        return elapsed

    start_dt = datetime.fromisoformat(t["start_sim_iso"])
    if start_dt.tzinfo is None:
        start_dt = TZ.localize(start_dt)
    diff = int(max(0, (clock_dt - start_dt).total_seconds()))
    return elapsed + diff

def set_timer_seconds(mode: str, timer_id: int, new_seconds: int, clock_dt: datetime):
    """
    Set timer to EXACT new_seconds.
    Works even while running (if allowed): keeps running state and resets "start" to now.
    """
    t = timer_row(mode, timer_id)
    if t is None:
        return False, "timer missing"

    running = int(t["running"]) == 1
    if running and not ALLOW_EDIT_WHILE_RUNNING:
        return False, "cannot edit while running"

    new_seconds = max(0, int(new_seconds))

    conn = db()
    cur = conn.cursor()

    if running and ALLOW_EDIT_WHILE_RUNNING:
        # Keep running, set base elapsed to new_seconds and reset start to now.
        if mode == "real":
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_epoch=?, start_sim_iso=NULL
                WHERE mode=? AND timer_id=?
            """, (new_seconds, tz_now_real().timestamp(), mode, timer_id))
        else:
            if clock_dt is None:
                clock_dt = get_sim_now()
            if clock_dt is None:
                # if sim clock missing, stop edit
                conn.close()
                return False, "sim clock missing"
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_sim_iso=?, start_epoch=NULL
                WHERE mode=? AND timer_id=?
            """, (new_seconds, clock_dt.replace(tzinfo=None).isoformat(timespec="seconds"), mode, timer_id))
    else:
        # Not running: set elapsed and clear starts
        cur.execute("""
            UPDATE timers
            SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL
            WHERE mode=? AND timer_id=?
        """, (new_seconds, mode, timer_id))

    conn.commit()
    conn.close()
    return True, "ok"

# =========================
# GOOGLE SHEETS
# =========================
WS = None

def gs_connect():
    global WS
    if WS is not None:
        return WS

    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        WS = None
        return None

    import gspread
    from google.oauth2.service_account import Credentials

    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)
    WS = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
    return WS

def target_hour_and_date(dt: datetime):
    """
    Rules:
    - 00:00–07:59 -> previous day @ 23
    - hour > 23 -> clamp to 23
    - else -> dt.hour
    """
    if dt.hour < FIRST_LOG_HOUR:
        return 23, (dt.date() - timedelta(days=1))
    if dt.hour > 23:
        return 23, dt.date()
    return dt.hour, dt.date()

def sheet_row_for_hour(hour: int) -> int:
    # row 7 = 08:00, row 22 = 23:00
    row = 7 + (hour - 8)
    if row < 7:
        row = 7
    if row > 22:
        row = 22
    return row

def write_two_timers_into_sheet(ws, row: int, col: int, mode: str, clock_dt: datetime):
    """
    ✅ FIX: two separate cells (Timer1 in col, Timer2 in col+1)
    """
    t1 = fmt(timer_total_seconds(mode, 1, clock_dt))
    t2 = fmt(timer_total_seconds(mode, 2, clock_dt))
    ws.update_cell(row, col, t1)
    ws.update_cell(row, col + 1, t2)

def log_to_sheet(mode: str, clock_dt: datetime, force=False):
    ws = gs_connect()
    if ws is None:
        return False, "Google creds missing"

    if clock_dt is None:
        return False, "clock missing"

    hour, day = target_hour_and_date(clock_dt)
    day_str = day.isoformat()

    # prevent duplicates on auto unless forced
    key_h = f"last_auto_logged_hour_{mode}"
    key_d = f"last_auto_logged_day_{mode}"
    if not force:
        if get_meta(key_h) == str(hour) and get_meta(key_d) == day_str:
            return True, "already logged"

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return False, f"Date {date_str} not found in sheet row 3"

    col = headers.index(date_str) + 1
    row = sheet_row_for_hour(hour)

    write_two_timers_into_sheet(ws, row, col, mode, clock_dt)

    # store last auto log marker
    set_meta(key_h, str(hour))
    set_meta(key_d, day_str)
    return True, "logged"

def get_activity_time_for_day(ws, day):
    """
    מחזיר את זמן הפעילות היומי (שורה 23–24) עבור יום נתון
    """
    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)

    if date_str not in headers:
        return None

    col = headers.index(date_str) + 1
    row = 22  # שורה 23–24 (08=7 ... 23=22)

    value = ws.cell(row, col).value
    return value or "00:00:00"


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
    service = build("calendar", "v3", credentials=creds)
    return service


def update_calendar_daily_summary(calendar_id: str, day, activity_time: str):
    """
    מעדכן את אירוע 'סיכום יום' בתאריך נתון
    """
    service = get_calendar_service()
    if service is None:
        return False

    start = datetime.combine(day, datetime.min.time()).astimezone(TZ)
    end = start + timedelta(days=1)

    events = service.events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True
    ).execute().get("items", [])

    for ev in events:
        if ev.get("summary") == "סיכום יום":
            desc = ev.get("description", "")

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



def should_auto_log_for_mode(mode: str, clock_dt: datetime) -> bool:
    """
    ✅ Do not miss even if request arrives at :38
    We log once per (hour,day) when hour is within 08..24 (24 treated as 23).
    """
    if clock_dt is None:
        return False
    hour = clock_dt.hour
    if hour < FIRST_LOG_HOUR and hour >= 0:
        # still can log, but your rule is "between 08–24" only
        return False
    if hour > LAST_LOG_HOUR:
        return False
    return True

# =========================
# START TIME LOG (Row 4) - PER MODE
# =========================
def log_start_time_if_needed(mode: str, clock_dt: datetime):
    """
    Write start time (HH:MM) to row 4 for the DATE of clock_dt,
    first Start after 05:00. Works for real and sim.
    One per day per mode.
    """
    if clock_dt is None:
        return
    if clock_dt.hour < RESET_HOUR:
        return

    ws = gs_connect()
    if ws is None:
        return

    _, day = target_hour_and_date(clock_dt)
    day_key = day.isoformat()

    key = f"first_start_logged_date_{mode}"
    if get_meta(key) == day_key:
        return

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    ws.update_cell(4, col, clock_dt.strftime("%H:%M"))
    set_meta(key, day_key)

# =========================
# ROUTES (UI)
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")

# =========================
# ROUTES (API)
# =========================
@app.route("/api/status")
def status():
    # advance sim clock by 1 sec per status tick
    if sim_enabled():
        s = get_sim_now()
        if s is not None:
            set_sim_now(s + timedelta(seconds=1))

    # ensure resets (both modes)
    ensure_daily_reset_for_mode("real", tz_now_real())
    ensure_daily_reset_for_mode("sim", get_sim_now())

    # auto log for real & sim (independent)
    real_clock = tz_now_real()
    sim_clock = get_sim_now()

    if should_auto_log_for_mode("real", real_clock):
        # log if (hour/day) changed since last auto
        h, d = target_hour_and_date(real_clock)
        if get_meta("last_auto_logged_hour_real") != str(h) or get_meta("last_auto_logged_day_real") != d.isoformat():
            log_to_sheet("real", real_clock, force=False)

    if sim_enabled() and should_auto_log_for_mode("sim", sim_clock):
        h, d = target_hour_and_date(sim_clock)
        if get_meta("last_auto_logged_hour_sim") != str(h) or get_meta("last_auto_logged_day_sim") != d.isoformat():
            log_to_sheet("sim", sim_clock, force=False)

    # which timers to display? current mode
    mode = current_mode()
    clock = now_for_mode(mode)
# =========================
    # DAILY CALENDAR SUMMARY (00:30)
    # =========================
    now_real = tz_now_real()

    if should_update_daily_summary(now_real):
        yesterday = now_real.date() - timedelta(days=1)

        ws = gs_connect()
        if ws:
            activity = get_activity_time_for_day(ws, yesterday)
            if activity:
                update_calendar_daily_summary(
                    calendar_id="primary",
                    day=yesterday,
                    activity_time=activity
                )

    timers = [fmt(timer_total_seconds(mode, i, clock)) for i in range(1, TIMER_COUNT + 1)]

    return jsonify({
        "now_str": (clock.strftime("%d/%m/%Y %H:%M:%S") if clock else ""),
        "simulation": sim_enabled(),
        "mode": mode,
        "timers": timers,
        "last_log": {
            "mode": get_meta("last_log_mode"),
            "ok": get_meta("last_log_ok"),
            "msg": get_meta("last_log_msg"),
            "at": get_meta("last_log_at_iso"),
        }
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock = now_for_mode(mode)

    # start-time write on first Start after 05:00 (any timer triggers it)
    log_start_time_if_needed(mode, clock)

    t = timer_row(mode, i)
    if t is None:
        return jsonify(error="timer missing"), 500

    if int(t["running"]) == 1:
        return ("", 204)

    conn = db()
    cur = conn.cursor()
    if mode == "real":
        cur.execute("""
            UPDATE timers
            SET running=1, start_epoch=?, start_sim_iso=NULL
            WHERE mode=? AND timer_id=?
        """, (tz_now_real().timestamp(), mode, i))
    else:
        if clock is None:
            return jsonify(error="sim clock missing"), 400
        cur.execute("""
            UPDATE timers
            SET running=1, start_sim_iso=?, start_epoch=NULL
            WHERE mode=? AND timer_id=?
        """, (clock.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))

    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock = now_for_mode(mode)
    total = timer_total_seconds(mode, i, clock)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE timers
        SET running=0, elapsed=?, start_epoch=NULL, start_sim_iso=NULL
        WHERE mode=? AND timer_id=?
    """, (total, mode, i))
    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/reset", methods=["POST"])
def reset_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE timers
        SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
        WHERE mode=? AND timer_id=?
    """, (mode, i))
    conn.commit()
    conn.close()
    return ("", 204)

# ----- Adjust (+5 / -10) -----
@app.route("/api/timer/<int:i>/adjust", methods=["POST"])
def adjust_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    data = request.get_json(force=True)
    delta = int(data.get("delta", 0))

    mode = current_mode()
    clock = now_for_mode(mode)
    current = timer_total_seconds(mode, i, clock)
    ok, msg = set_timer_seconds(mode, i, current + delta, clock)
    if not ok:
        return jsonify(ok=False, error=msg), 400

    # return new formatted time
    new_total = timer_total_seconds(mode, i, clock)
    return jsonify(ok=True, new_time=fmt(new_total))

# ----- Set exact time -----
@app.route("/api/timer/<int:i>/set", methods=["POST"])
def set_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    data = request.get_json(force=True)
    seconds = int(data.get("seconds", 0))

    mode = current_mode()
    clock = now_for_mode(mode)
    ok, msg = set_timer_seconds(mode, i, seconds, clock)
    if not ok:
        return jsonify(ok=False, error=msg), 400

    new_total = timer_total_seconds(mode, i, clock)
    return jsonify(ok=True, new_time=fmt(new_total))

# ----- Manual log now (for current mode) -----
@app.route("/api/log-now", methods=["POST"])

@app.route("/api/debug/daily-summary", methods=["GET"])
def debug_daily_summary():
    now_real = tz_now_real()
    yesterday = now_real.date() - timedelta(days=1)

    print("DEBUG daily summary")
    print("now_real:", now_real)
    print("yesterday:", yesterday)

    ws = gs_connect()
    if not ws:
        return jsonify(ok=False, error="no sheet connection")

    activity = get_activity_time_for_day(ws, yesterday)

    print("activity from sheet:", activity)

    return jsonify(
        ok=True,
        now=str(now_real),
        yesterday=str(yesterday),
        activity=activity
    )

def manual_log():
    mode = current_mode()
    clock = now_for_mode(mode)

    ok, msg = log_to_sheet(mode, clock, force=True)

    # persist for UI
    set_meta("last_log_mode", mode)
    set_meta("last_log_ok", "1" if ok else "0")
    set_meta("last_log_msg", msg)
    set_meta("last_log_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    return jsonify(ok=ok, message=msg), (200 if ok else 500)

# ----- Simulation -----
@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    data = request.get_json(force=True)
    dt = datetime.strptime(data["datetime"], "%Y-%m-%d %H:%M")
    dt = TZ.localize(dt)
    set_meta("sim_enabled", "1")
    set_sim_now(dt)
    return jsonify(ok=True)

@app.route("/api/sim/stop", methods=["POST"])

def sim_stop():
    set_meta("sim_enabled", "0")
    set_meta("sim_now_iso", "")
    return jsonify(ok=True)

@app.route("/api/test-calendar", methods=["GET", "POST"])
def test_calendar():
    ws = gs_connect()
    if ws is None:
        return jsonify(error="no sheet"), 500

    yesterday = tz_now_real().date() - timedelta(days=1)
    activity = get_activity_time_for_day(ws, yesterday)

    if not activity:
        return jsonify(error="no activity"), 404

    ok = update_calendar_daily_summary(
        calendar_id="primary",
        day=yesterday,
        activity_time=activity
    )

    return jsonify(ok=ok, activity=activity)


if __name__ == "__main__":
    app.run(debug=True)