import os
import json
import sqlite3
from datetime import datetime, timedelta, date, time as dtime

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
FIRST_LOG_HOUR = 8      # auto log window start (08)
LAST_LOG_HOUR = 24      # for clarity; actual datetime hours are 0..23

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

ALLOW_EDIT_WHILE_RUNNING = True

DB_PATH = os.getenv("DB_PATH", "/tmp/work_timers.db")

# Google creds must be in env as JSON string
# GOOGLE_CREDS_JSON='{"type":"service_account", ... }'
GOOGLE_CREDS_ENV = "GOOGLE_CREDS_JSON"

# =========================
# SQLITE
# =========================
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
        elapsed INTEGER NOT NULL DEFAULT 0, -- seconds when not running
        start_epoch REAL,                   -- unix epoch when started (real)
        start_sim_iso TEXT,                 -- local ISO (no tz) when started (sim)
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
        # simulation clock
        "sim_enabled": "0",
        "sim_now_iso": "",

        # UI manual log feedback
        "last_log_mode": "",
        "last_log_ok": "",
        "last_log_msg": "",
        "last_log_at_iso": "",

        # auto log guards (per mode)
        "last_auto_logged_hour_real": "",
        "last_auto_logged_day_real": "",
        "last_auto_logged_hour_sim": "",
        "last_auto_logged_day_sim": "",

        # daily reset guards
        "last_reset_date_real": "",
        "last_reset_date_sim": "",

        # start-time written guards
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",

        # calendar daily summary guard
        "daily_calendar_updated_date": "",

        # last calendar status for UI
        "calendar_ok": "",
        "calendar_msg": "",
        "calendar_at_iso": "",

        # last sheet status for UI
        "sheet_ok": "",
        "sheet_msg": "",
        "sheet_at_iso": "",
    }

    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (k, v))

    conn.commit()
    conn.close()


init_db()


# =========================
# META
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
def tz_now_real():
    return datetime.now(TZ)


def sim_enabled() -> bool:
    return get_meta("sim_enabled") == "1"


def get_sim_now():
    iso = get_meta("sim_now_iso")
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)  # stored without tzinfo
    return TZ.localize(dt)


def set_sim_now(dt: datetime):
    dt = dt.astimezone(TZ)
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))


def now_for_mode(mode: str):
    return get_sim_now() if mode == "sim" else tz_now_real()


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

    today = clock_dt.date().isoformat()
    key_last = f"last_reset_date_{mode}"
    key_start = f"first_start_logged_date_{mode}"

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
    set_meta(key_start, "")  # allow start-time write again


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
    start_dt = TZ.localize(datetime.fromisoformat(t["start_sim_iso"]))
    diff = int(max(0, (clock_dt - start_dt).total_seconds()))
    return elapsed + diff


def set_timer_seconds(mode: str, timer_id: int, new_seconds: int, clock_dt: datetime):
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
        # keep running, reset start to now
        if mode == "real":
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_epoch=?, start_sim_iso=NULL
                WHERE mode=? AND timer_id=?
            """, (new_seconds, tz_now_real().timestamp(), mode, timer_id))
        else:
            if clock_dt is None:
                return False, "sim clock missing"
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_sim_iso=?, start_epoch=NULL
                WHERE mode=? AND timer_id=?
            """, (
                new_seconds,
                clock_dt.replace(tzinfo=None).isoformat(timespec="seconds"),
                mode,
                timer_id
            ))
    else:
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

    raw = os.getenv(GOOGLE_CREDS_ENV)
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
    t1 = fmt(timer_total_seconds(mode, 1, clock_dt))
    t2 = fmt(timer_total_seconds(mode, 2, clock_dt))
    ws.update_cell(row, col, t1)
    ws.update_cell(row, col + 1, t2)


def log_to_sheet(mode: str, clock_dt: datetime, force=False):
    ws = gs_connect()
    if ws is None:
        return False, "Google creds missing / sheet not connected"
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

    set_meta(key_h, str(hour))
    set_meta(key_d, day_str)
    return True, f"logged hour={hour} day={day_str}"


def log_start_time_if_needed(mode: str, clock_dt: datetime):
    """
    Write start time (HH:MM) to row 4 for the DATE of clock_dt,
    first Start after 05:00. One per day per mode.
    """
    if clock_dt is None:
        return True, "clock missing (skip)"
    if clock_dt.hour < RESET_HOUR:
        return True, "before 05:00 (skip)"

    ws = gs_connect()
    if ws is None:
        return False, "sheet not connected"

    _, day = target_hour_and_date(clock_dt)  # respects after-midnight rule
    day_key = day.isoformat()
    key = f"first_start_logged_date_{mode}"

    if get_meta(key) == day_key:
        return True, "already wrote start time"

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return False, f"Date {date_str} not found in sheet row 3"

    col = headers.index(date_str) + 1
    ws.update_cell(4, col, clock_dt.strftime("%H:%M"))
    set_meta(key, day_key)
    return True, "start time written"


def get_activity_time_for_day(ws, day: date):
    """
    Daily activity time: value at row 22 (23:00–24:00) for that day
    """
    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return None

    col = headers.index(date_str) + 1
    row = 22
    value = ws.cell(row, col).value
    return value or "00:00:00"


# =========================
# GOOGLE CALENDAR
# =========================
from googleapiclient.discovery import build
from google.oauth2.service_account import Credentials

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def get_calendar_service():
    raw = os.getenv(GOOGLE_CREDS_ENV)
    if not raw:
        return None
    info = json.loads(raw)
    creds = Credentials.from_service_account_info(info, scopes=CALENDAR_SCOPES)
    return build("calendar", "v3", credentials=creds)


def find_event_on_day(calendar_id: str, day: date, summary_exact: str = "סיכום יום"):
    """
    Finds an event with exact summary on the given local day.
    Supports recurring events by using singleEvents=True (instances).
    """
    service = get_calendar_service()
    if service is None:
        return None, "calendar service not ready"

    start = TZ.localize(datetime.combine(day, dtime(0, 0, 0)))
    end = start + timedelta(days=1)

    items = service.events().list(
        calendarId=calendar_id,
        timeMin=start.isoformat(),
        timeMax=end.isoformat(),
        singleEvents=True,
        orderBy="startTime",
        maxResults=250
    ).execute().get("items", [])

    for ev in items:
        if ev.get("summary") == summary_exact:
            return ev, "found"
    return None, "event not found"


def update_calendar_daily_summary(calendar_id: str, day: date, activity_time: str):
    """
    Updates the 'סיכום יום' event instance on that day:
    adds/replaces line: זמן שהיית בפעילות- HH:MM:SS
    """
    service = get_calendar_service()
    if service is None:
        return False, "calendar service not ready"

    ev, msg = find_event_on_day(calendar_id, day, "סיכום יום")
    if ev is None:
        return False, msg

    desc = (ev.get("description", "") or "")
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

    return True, "updated"


# =========================
# AUTO POLICIES
# =========================
def should_auto_log_tick(dt: datetime) -> bool:
    """
    Auto log only at hour boundary.
    We allow a small second window so UI polling won't miss it.
    """
    if dt is None:
        return False
    return dt.minute == 0 and dt.second <= 5


def should_auto_log_for_mode(dt: datetime) -> bool:
    """
    Log window: 08:00..23:59
    (after midnight rule is handled by target_hour_and_date)
    """
    if dt is None:
        return False
    return FIRST_LOG_HOUR <= dt.hour <= 23


def should_update_daily_summary(now_dt: datetime) -> bool:
    """
    True once per day at 00:30 (Israel time).
    Guarded by meta.
    """
    if now_dt.hour == 0 and now_dt.minute == 30 and now_dt.second <= 10:
        today = now_dt.date().isoformat()
        last = get_meta("daily_calendar_updated_date")
        if last != today:
            set_meta("daily_calendar_updated_date", today)
            return True
    return False


# =========================
# UI ROUTES
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")


# =========================
# API ROUTES
# =========================
@app.route("/api/status")
def status():
    # advance sim clock by 1 sec per status tick
    if sim_enabled():
        s = get_sim_now()
        if s is not None:
            set_sim_now(s + timedelta(seconds=1))

    # ensure resets
    ensure_daily_reset_for_mode("real", tz_now_real())
    ensure_daily_reset_for_mode("sim", get_sim_now())

    # auto sheet log (hourly)
    real_clock = tz_now_real()
    sim_clock = get_sim_now()

    # real
    if should_auto_log_tick(real_clock) and should_auto_log_for_mode(real_clock):
        h, d = target_hour_and_date(real_clock)
        if get_meta("last_auto_logged_hour_real") != str(h) or get_meta("last_auto_logged_day_real") != d.isoformat():
            ok, msg = log_to_sheet("real", real_clock, force=False)
            set_meta("sheet_ok", "1" if ok else "0")
            set_meta("sheet_msg", msg)
            set_meta("sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    # sim (if enabled)
    if sim_enabled() and sim_clock and should_auto_log_tick(sim_clock) and should_auto_log_for_mode(sim_clock):
        h, d = target_hour_and_date(sim_clock)
        if get_meta("last_auto_logged_hour_sim") != str(h) or get_meta("last_auto_logged_day_sim") != d.isoformat():
            ok, msg = log_to_sheet("sim", sim_clock, force=False)
            set_meta("sheet_ok", "1" if ok else "0")
            set_meta("sheet_msg", msg)
            set_meta("sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    # daily calendar summary at 00:30 (real clock)
    now_real = tz_now_real()
    if should_update_daily_summary(now_real):
        try:
            yesterday = now_real.date() - timedelta(days=1)
            ws = gs_connect()
            if ws:
                activity = get_activity_time_for_day(ws, yesterday)
                if activity:
                    ok, msg = update_calendar_daily_summary("primary", yesterday, activity)
                    set_meta("calendar_ok", "1" if ok else "0")
                    set_meta("calendar_msg", msg)
                    set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
                else:
                    set_meta("calendar_ok", "0")
                    set_meta("calendar_msg", "activity not found in sheet")
                    set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
            else:
                set_meta("calendar_ok", "0")
                set_meta("calendar_msg", "sheet not connected")
                set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
        except Exception as e:
            set_meta("calendar_ok", "0")
            set_meta("calendar_msg", f"calendar error: {type(e).__name__}: {e}")
            set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))

    # display current mode timers
    mode = current_mode()
    clock = now_for_mode(mode)
    timers = [fmt(timer_total_seconds(mode, i, clock)) for i in range(1, TIMER_COUNT + 1)]

    return jsonify({
        "now_str": (clock.strftime("%d/%m/%Y %H:%M:%S") if clock else ""),
        "simulation": sim_enabled(),
        "mode": mode,
        "timers": timers,
        "sheet": {
            "ok": get_meta("sheet_ok"),
            "msg": get_meta("sheet_msg"),
            "at": get_meta("sheet_at_iso"),
        },
        "calendar": {
            "ok": get_meta("calendar_ok"),
            "msg": get_meta("calendar_msg"),
            "at": get_meta("calendar_at_iso"),
        },
        "last_log": {
            "mode": get_meta("last_log_mode"),
            "ok": get_meta("last_log_ok"),
            "msg": get_meta("last_log_msg"),
            "at": get_meta("last_log_at_iso"),
        },
    })


@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock = now_for_mode(mode)

    # Write start time (row 4) on first start after 05:00
    # IMPORTANT: Start must not fail because of sheet issues
    try:
        ok, msg = log_start_time_if_needed(mode, clock)
        # store into sheet status (non-blocking)
        set_meta("sheet_ok", "1" if ok else "0")
        set_meta("sheet_msg", msg)
        set_meta("sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))
    except Exception as e:
        set_meta("sheet_ok", "0")
        set_meta("sheet_msg", f"start-time write skipped: {type(e).__name__}: {e}")
        set_meta("sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

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

    new_total = timer_total_seconds(mode, i, clock)
    return jsonify(ok=True, new_time=fmt(new_total))


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


@app.route("/api/log-now", methods=["POST"])
def manual_log():
    """
    Manual write to Google Sheet + UI feedback
    """
    mode = current_mode()
    clock = now_for_mode(mode)

    ok, msg = log_to_sheet(mode, clock, force=True)

    set_meta("last_log_mode", mode)
    set_meta("last_log_ok", "1" if ok else "0")
    set_meta("last_log_msg", msg)
    set_meta("last_log_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    # also update sheet status section
    set_meta("sheet_ok", "1" if ok else "0")
    set_meta("sheet_msg", msg)
    set_meta("sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    return jsonify(ok=ok, message=msg), (200 if ok else 500)


@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    """
    Start simulation clock at provided datetime (Israel time).
    JSON: {"datetime": "YYYY-MM-DD HH:MM"}
    """
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


# =========================
# CALENDAR ENDPOINTS
# =========================
@app.route("/api/calendar/update-now", methods=["POST"])
def manual_calendar_update():
    """
    Manual calendar update (like 00:30)
    """
    now_real = tz_now_real()
    yesterday = now_real.date() - timedelta(days=1)

    ws = gs_connect()
    if not ws:
        set_meta("calendar_ok", "0")
        set_meta("calendar_msg", "sheet not connected")
        set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
        return jsonify(ok=False, error="sheet not connected"), 500

    activity = get_activity_time_for_day(ws, yesterday)
    if not activity:
        set_meta("calendar_ok", "0")
        set_meta("calendar_msg", "no activity found")
        set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
        return jsonify(ok=False, error="no activity found"), 404

    try:
        ok, msg = update_calendar_daily_summary("primary", yesterday, activity)
        set_meta("calendar_ok", "1" if ok else "0")
        set_meta("calendar_msg", msg)
        set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
        return jsonify(ok=ok, day=str(yesterday), activity=activity, message=msg), (200 if ok else 500)
    except Exception as e:
        set_meta("calendar_ok", "0")
        set_meta("calendar_msg", f"calendar error: {type(e).__name__}: {e}")
        set_meta("calendar_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
        return jsonify(ok=False, error=str(e)), 500


@app.route("/api/debug/calendars", methods=["GET"])
def debug_calendars():
    """
    🧪 Returns calendars visible to service account
    """
    service = get_calendar_service()
    if service is None:
        return jsonify(ok=False, error="calendar service not ready"), 500

    items = service.calendarList().list().execute().get("items", [])
    return jsonify(ok=True, calendars=[{"id": c.get("id"), "summary": c.get("summary")} for c in items])


@app.route("/api/debug/find-summary", methods=["GET"])
def debug_find_summary():
    """
    🧪 Checks where 'סיכום יום' exists for a given date across all calendars.
    Query: ?date=YYYY-MM-DD  (default yesterday)
    """
    service = get_calendar_service()
    if service is None:
        return jsonify(ok=False, error="calendar service not ready"), 500

    qd = request.args.get("date", "")
    if qd:
        day = datetime.strptime(qd, "%Y-%m-%d").date()
    else:
        day = (tz_now_real().date() - timedelta(days=1))

    cals = service.calendarList().list().execute().get("items", [])

    found = []
    for c in cals:
        cal_id = c.get("id")
        cal_sum = c.get("summary")
        ev, msg = find_event_on_day(cal_id, day, "סיכום יום")
        if ev is not None:
            found.append({
                "calendar_id": cal_id,
                "calendar_summary": cal_sum,
                "event_id": ev.get("id"),
                "event_summary": ev.get("summary"),
            })

    return jsonify(
        ok=True,
        checked_day=str(day),
        calendars_checked=len(cals),
        found=found
    )


# =========================
# RUN
# =========================
if __name__ == "__main__":
    app.run(debug=True)