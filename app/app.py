# app.py
import os
import json
import sqlite3
import traceback
from datetime import datetime, timedelta, date, time as dtime

import pytz
from flask import Flask, jsonify, render_template, request

# ✅ templates/ יושב ליד app.py בתוך app/templates
app = Flask(__name__, template_folder="templates")
route
# =========================
# TIMEZONE
# =========================
TZ = pytz.timezone("Asia/Jerusalem")

def tz_now_real() -> datetime:
    return datetime.now(TZ)

# =========================
# CONFIG
# =========================
TIMER_COUNT = 2

RESET_HOUR = 5           # daily reset at 05:00
FIRST_LOG_HOUR = 8       # 08:00..23:59; וגם 00:00-07:59 נכתב ל-23 של יום קודם

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# True  -> allow Set/+5/-10 while running
# False -> block edits while running
ALLOW_EDIT_WHILE_RUNNING = True

# Calendar
CALENDAR_SUMMARY = os.getenv("CALENDAR_SUMMARY", "סיכום יום")
CALENDAR_ID = os.getenv("CALENDAR_ID", "").strip()

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
        # sim clock
        "sim_enabled": "0",
        "sim_now_iso": "",

        # last manual/auto sheet log result (for UI)
        "last_sheet_mode": "",
        "last_sheet_ok": "0",
        "last_sheet_msg": "",
        "last_sheet_at_iso": "",

        # last calendar update result (for UI)
        "last_cal_ok": "0",
        "last_cal_msg": "",
        "last_cal_at_iso": "",
        "last_cal_calendar_id": "",
        "last_cal_event_id": "",

        # AUTO sheet log guards (per mode)
        "last_auto_logged_key_real": "",  # e.g. "2026-01-06|23"
        "last_auto_logged_key_sim": "",

        # resets (per mode)
        "last_reset_date_real": "",       # YYYY-MM-DD
        "last_reset_date_sim": "",

        # start-time written (per mode) - day key YYYY-MM-DD
        "first_start_logged_date_real": "",
        "first_start_logged_date_sim": "",

        # daily calendar guard (once per day)
        "daily_calendar_updated_date": "",  # YYYY-MM-DD
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
# SIMULATION CLOCK
# =========================
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
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now_for_mode(mode: str):
    return get_sim_now() if mode == "sim" else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# DAILY RESET (05:00) - PER MODE
# =========================
def ensure_daily_reset_for_mode(mode: str, clock_dt: datetime):
    if clock_dt is None:
        return
    if clock_dt.hour < RESET_HOUR:
        return

    key_last = f"last_reset_date_{mode}"
    key_start = f"first_start_logged_date_{mode}"

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
# sim
    if t["start_sim_iso"] is None or clock_dt is None:
        return elapsed

    # ✅ אם הטיימר לא רץ (DONE / PENDING) – הזמן קפוא
    if int(t["running"]) == 0:
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
                conn.close()
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
# GOOGLE CREDS
# =========================
def _load_sa_info():
    raw = os.getenv("GOOGLE_CREDS_JSON")
    if not raw:
        return None
    return json.loads(raw)

# =========================
# GOOGLE SHEETS
# =========================
WS = None

def gs_connect():
    global WS
    if WS is not None:
        return WS

    info = _load_sa_info()
    if not info:
        WS = None
        return None

    import gspread
    from google.oauth2.service_account import Credentials

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
    - else -> dt.hour
    """
    if dt.hour < FIRST_LOG_HOUR:
        return 23, (dt.date() - timedelta(days=1))
    return dt.hour, dt.date()

def sheet_row_for_hour(hour: int) -> int:
    # row 7 = 08:00, row 22 = 23:00
    if hour < 8:
        hour = 8
    if hour > 23:
        hour = 23
    return 7 + (hour - 8)

def write_two_timers_into_sheet(ws, row: int, col: int, mode: str, clock_dt: datetime):
    """
    writes 2 timers:
    Timer1 in col, Timer2 in col+1
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
    if hour > 23:
        hour = 23

    day_str = day.isoformat()
    key = f"{day_str}|{hour}"
    key_meta = f"last_auto_logged_key_{mode}"

    if not force:
        if get_meta(key_meta) == key:
            return True, "already logged"

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return False, f"Date {date_str} not found in sheet row 3"

    col = headers.index(date_str) + 1
    row = sheet_row_for_hour(hour)

    write_two_timers_into_sheet(ws, row, col, mode, clock_dt)

    set_meta(key_meta, key)
    return True, f"logged (hour={hour} day={day_str} row={row} col={col})"

def log_start_time_if_needed(mode: str, clock_dt: datetime):
    """
    Write start time (HH:MM) to row 4 for the DATE of clock_dt,
    first Start after 05:00. One per day per mode.
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

def get_activity_time_for_day(ws, day: date):
    """
    זמן פעילות יומי: הערך בשורת 23:00–24:00 (row 22) עבור היום
    """
    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return None

    col = headers.index(date_str) + 1
    row = 22  # 23:00–24:00
    value = ws.cell(row, col).value
    return value or "00:00:00"

# =========================
# ✅ AUTO LOG "סלחני" (התיקון שביקשת)
# =========================
def auto_log_if_needed(mode: str, clock_dt: datetime):
    """
    במקום להסתמך על דקה/שנייה מדויקות:
    בכל קריאה ל-/api/status נבדוק האם כבר נכתב עדכון לשעה הרלוונטית.
    אם לא -> נכתוב עכשיו.
    """
    if clock_dt is None:
        return None

    hour, day = target_hour_and_date(clock_dt)
    if hour > 23:
        hour = 23

    key = f"{day.isoformat()}|{hour}"
    meta_key = f"last_auto_logged_key_{mode}"
    if get_meta(meta_key) == key:
        return None  # כבר עודכן לשעה הזו

    ok, msg = log_to_sheet(mode, clock_dt, force=True)

    # שמירה ל-UI (כמו ביומן)
    set_meta("last_sheet_mode", mode)
    set_meta("last_sheet_ok", "1" if ok else "0")
    set_meta("last_sheet_msg", msg)
    set_meta("last_sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    return {
        "ok": ok,
        "msg": msg,
        "mode": mode,
        "at": get_meta("last_sheet_at_iso"),
    }

# =========================
# GOOGLE CALENDAR
# =========================
def get_calendar_service():
    info = _load_sa_info()
    if not info:
        return None

    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("calendar", "v3", credentials=creds)

def _day_range_iso(day: date):
    start_local = TZ.localize(datetime.combine(day, dtime(0, 0, 0)))
    end_local = start_local + timedelta(days=1)
    return start_local.isoformat(), end_local.isoformat()

def list_calendars():
    service = get_calendar_service()
    if service is None:
        return False, "calendar creds missing", []

    items = []
    page_token = None
    while True:
        resp = service.calendarList().list(pageToken=page_token).execute()
        for cal in resp.get("items", []):
            items.append({
                "id": cal.get("id"),
                "summary": cal.get("summary"),
                "primary": cal.get("primary", False),
                "accessRole": cal.get("accessRole"),
            })
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return True, "ok", items

def find_summary_event(calendar_id: str, day: date, summary: str):
    service = get_calendar_service()
    if service is None:
        return False, calendar_id, "", None, "calendar creds missing"

    timeMin, timeMax = _day_range_iso(day)
    events = service.events().list(
        calendarId=calendar_id,
        timeMin=timeMin,
        timeMax=timeMax,
        singleEvents=True,
        orderBy="startTime",
    ).execute().get("items", [])

    for ev in events:
        if (ev.get("summary") or "").strip() == summary:
            return True, calendar_id, ev.get("id", ""), ev, "found"
    return False, calendar_id, "", None, "event not found"

def update_calendar_daily_summary(day: date, activity_time: str):
    target_calendar_id = CALENDAR_ID
    used_calendar_id = ""
    ev = None
    ev_id = ""

    if not target_calendar_id:
        ok, msg, cals = list_calendars()
        if not ok:
            return False, "", "", msg

        ordered = sorted(cals, key=lambda x: (not x.get("primary", False)))
        for cal in ordered:
            cid = cal["id"]
            found, used_calendar_id, ev_id, ev, fmsg = find_summary_event(cid, day, CALENDAR_SUMMARY)
            if found:
                break
        if ev is None:
            return False, "", "", "event not found"
    else:
        found, used_calendar_id, ev_id, ev, fmsg = find_summary_event(target_calendar_id, day, CALENDAR_SUMMARY)
        if not found:
            return False, used_calendar_id, "", fmsg

    service = get_calendar_service()
    if service is None:
        return False, used_calendar_id, ev_id, "calendar creds missing"

    desc = (ev.get("description", "") or "")
    lines = desc.splitlines()

    line_prefix = "זמן שהיית בפעילות"
    new_line = f"זמן שהיית בפעילות- {activity_time}"

    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(line_prefix):
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)

    ev["description"] = "\n".join(lines)

    service.events().update(
        calendarId=used_calendar_id,
        eventId=ev_id,
        body=ev
    ).execute()

    return True, used_calendar_id, ev_id, "updated"

def should_update_daily_summary(now_dt: datetime) -> bool:
    """
    True פעם אחת ביום ב-00:30 (לפי שעון ישראל)
    """
    if now_dt.hour == 0 and now_dt.minute == 30:
        last = get_meta("daily_calendar_updated_date")
        today = now_dt.date().isoformat()
        if last != today:
            set_meta("daily_calendar_updated_date", today)
            return True
    return False

# =========================
# UI
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")

# =========================
# API: STATUS
# =========================
@app.route("/api/status")
def status():
    # tick sim by 1 sec per status call
    if sim_enabled():
        s = get_sim_now()
        if s is not None:
            set_sim_now(s + timedelta(seconds=1))

    # resets (both modes)
    ensure_daily_reset_for_mode("real", tz_now_real())
    ensure_daily_reset_for_mode("sim", get_sim_now())

    # default infos (for UI)
    sheet_info = {
        "ok": get_meta("last_sheet_ok") == "1",
        "msg": get_meta("last_sheet_msg"),
        "at": get_meta("last_sheet_at_iso"),
        "mode": get_meta("last_sheet_mode"),
    }
    cal_info = {
        "ok": get_meta("last_cal_ok") == "1",
        "msg": get_meta("last_cal_msg"),
        "at": get_meta("last_cal_at_iso"),
        "calendar_id": get_meta("last_cal_calendar_id"),
        "event_id": get_meta("last_cal_event_id"),
    }

    # ✅ AUTO SHEET LOG "סלחני" – REAL
    try:
        res = auto_log_if_needed("real", tz_now_real())
        if res:
            sheet_info = res
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        set_meta("last_sheet_ok", "0")
        set_meta("last_sheet_msg", msg)
        set_meta("last_sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))
        set_meta("last_sheet_mode", "real")
        sheet_info = {
            "ok": False,
            "msg": msg,
            "at": get_meta("last_sheet_at_iso"),
            "mode": "real",
        }

    # ✅ AUTO SHEET LOG – SIM (אם פעיל)
    if sim_enabled():
        try:
            res = auto_log_if_needed("sim", get_sim_now())
            # אם הסים נכשל – נציג את הבעיה האחרונה
            if res and not res["ok"]:
                sheet_info = res
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            set_meta("last_sheet_ok", "0")
            set_meta("last_sheet_msg", msg)
            set_meta("last_sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))
            set_meta("last_sheet_mode", "sim")
            sheet_info = {
                "ok": False,
                "msg": msg,
                "at": get_meta("last_sheet_at_iso"),
                "mode": "sim",
            }

    # DAILY CALENDAR SUMMARY (00:30) - runs once per day (real)
    try:
        now_real = tz_now_real()
        if should_update_daily_summary(now_real):
            yesterday = now_real.date() - timedelta(days=1)
            ws = gs_connect()
            if ws is None:
                raise RuntimeError("no sheet connection for calendar summary")
            activity = get_activity_time_for_day(ws, yesterday)
            if not activity:
                raise RuntimeError("no activity found in sheet for yesterday")

            ok, cal_id, ev_id, msg = update_calendar_daily_summary(yesterday, activity)

            set_meta("last_cal_ok", "1" if ok else "0")
            set_meta("last_cal_msg", msg)
            set_meta("last_cal_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
            set_meta("last_cal_calendar_id", cal_id or "")
            set_meta("last_cal_event_id", ev_id or "")

            cal_info = {
                "ok": ok,
                "msg": msg,
                "at": get_meta("last_cal_at_iso"),
                "calendar_id": cal_id or "",
                "event_id": ev_id or "",
            }
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        set_meta("last_cal_ok", "0")
        set_meta("last_cal_msg", msg)
        set_meta("last_cal_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))
        cal_info = {
            "ok": False,
            "msg": msg,
            "at": get_meta("last_cal_at_iso"),
            "calendar_id": get_meta("last_cal_calendar_id"),
            "event_id": get_meta("last_cal_event_id"),
        }

    # which timers to display? current mode
    mode = current_mode()
    clock = now_for_mode(mode)
    timers = [fmt(timer_total_seconds(mode, i, clock)) for i in range(1, TIMER_COUNT + 1)]

    return jsonify({
        "now_str": (clock.strftime("%d/%m/%Y %H:%M:%S") if clock else ""),
        "simulation": sim_enabled(),
        "mode": mode,
        "timers": timers,
        "sheet": sheet_info,
        "calendar": cal_info,
    })

# =========================
# API: TIMER CONTROLS
# =========================
@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock = now_for_mode(mode)

    # ✅ שעת התחלה בשורה 4: לא מפיל Start אם יש בעיה
    try:
        log_start_time_if_needed(mode, clock)
    except Exception as e:
        print("⚠️ start-time write skipped:", type(e).__name__, str(e))

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


      
def stop_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock = now_for_mode(mode)

    conn = db()
    cur = conn.cursor()

    if mode == "sim":
        # DONE בסימולציה → התחלה = סיום
        cur.execute("""
            UPDATE timers
            SET running=0,
                elapsed=0,
                start_sim_iso=?
            WHERE mode=? AND timer_id=?
        """, (
            clock.replace(tzinfo=None).isoformat(timespec="seconds"),
            mode,
            i
        ))
    else:
        # REAL – נשאר כמו שהיה
        total = timer_total_seconds(mode, i, clock)
        cur.execute("""
            UPDATE timers
            SET running=0,
                elapsed=?
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

    data = request.get_json(force=True) or {}
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

    data = request.get_json(force=True) or {}
    seconds = int(data.get("seconds", 0))

    mode = current_mode()
    clock = now_for_mode(mode)

    ok, msg = set_timer_seconds(mode, i, seconds, clock)
    if not ok:
        return jsonify(ok=False, error=msg), 400

    new_total = timer_total_seconds(mode, i, clock)
    return jsonify(ok=True, new_time=fmt(new_total))

# =========================
# API: MANUAL SHEET LOG (כפתור)
# =========================
@app.route("/api/log-now", methods=["POST"])
def manual_log():
    mode = current_mode()
    clock = now_for_mode(mode)

    try:
        ok, msg = log_to_sheet(mode, clock, force=True)
    except Exception as e:
        ok, msg = False, f"{type(e).__name__}: {e}"

    set_meta("last_sheet_mode", mode)
    set_meta("last_sheet_ok", "1" if ok else "0")
    set_meta("last_sheet_msg", msg)
    set_meta("last_sheet_at_iso", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))

    return jsonify(ok=ok, message=msg), (200 if ok else 500)

# =========================
# API: SIMULATION
# =========================
@app.route("/api/sim/start", methods=["POST"])
def sim_start():
    data = request.get_json(force=True) or {}
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
# API: CALENDAR (manual update like 00:30)
# =========================
@app.route("/api/calendar/update-now", methods=["POST"])
def manual_calendar_update():
    now_real = tz_now_real()
    yesterday = now_real.date() - timedelta(days=1)

    try:
        ws = gs_connect()
        if not ws:
            return jsonify(ok=False, error="no sheet connection"), 500

        activity = get_activity_time_for_day(ws, yesterday)
        if not activity:
            return jsonify(ok=False, error="no activity found"), 404

        ok, cal_id, ev_id, msg = update_calendar_daily_summary(yesterday, activity)

        set_meta("last_cal_ok", "1" if ok else "0")
        set_meta("last_cal_msg", msg)
        set_meta("last_cal_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
        set_meta("last_cal_calendar_id", cal_id or "")
        set_meta("last_cal_event_id", ev_id or "")

        return jsonify(
            ok=ok,
            day=str(yesterday),
            activity=activity,
            calendar_id=cal_id,
            event_id=ev_id,
            message=msg
        ), (200 if ok else 500)

    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        set_meta("last_cal_ok", "0")
        set_meta("last_cal_msg", msg)
        set_meta("last_cal_at_iso", now_real.strftime("%d/%m/%Y %H:%M:%S"))
        return jsonify(ok=False, error=msg), 500

# =========================
# 🧪 Endpoint בדיקה: איזה יומן נמצא
# =========================
@app.route("/api/test/calendar-find", methods=["GET"])
def test_calendar_find():
    try:
        day_s = request.args.get("day", "").strip()
        if day_s:
            checked_day = date.fromisoformat(day_s)
        else:
            checked_day = tz_now_real().date() - timedelta(days=1)

        ok, msg, cals = list_calendars()
        if not ok:
            return jsonify(ok=False, error=msg), 500

        tried = []
        found = []
        default_calendar_id = CALENDAR_ID if CALENDAR_ID else "AUTO"

        if CALENDAR_ID:
            f, cid, eid, ev, fmsg = find_summary_event(CALENDAR_ID, checked_day, CALENDAR_SUMMARY)
            tried.append({"calendar_id": cid, "result": fmsg})
            if f:
                found.append({
                    "calendar_id": cid,
                    "event_id": eid,
                    "summary": ev.get("summary"),
                    "start": ev.get("start"),
                })
        else:
            ordered = sorted(cals, key=lambda x: (not x.get("primary", False)))
            for cal in ordered:
                cid = cal["id"]
                f, cid2, eid, ev, fmsg = find_summary_event(cid, checked_day, CALENDAR_SUMMARY)
                tried.append({"calendar_id": cid, "calendar_summary": cal.get("summary"), "result": fmsg})
                if f:
                    found.append({
                        "calendar_id": cid2,
                        "event_id": eid,
                        "summary": ev.get("summary"),
                        "start": ev.get("start"),
                    })
                    break

        return jsonify(
            ok=True,
            checked_day=str(checked_day),
            summary=CALENDAR_SUMMARY,
            calendar_id_config=default_calendar_id,
            found=found,
            tried=tried[:25]
        )

    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}",
                       trace=traceback.format_exc().splitlines()[-5:]), 500

# =========================
# Extra debug: list calendars accessible
# =========================
@app.route("/api/test/calendar-list", methods=["GET"])
def test_calendar_list():
    ok, msg, cals = list_calendars()
    if not ok:
        return jsonify(ok=False, error=msg), 500
    return jsonify(ok=True, calendars=cals)

# =========================
# Main
# =========================
if __name__ == "__main__":
    app.run(debug=True)
