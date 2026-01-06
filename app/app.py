# app/app.py
import os
import json
import time
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, date, timedelta

import pytz
from flask import Flask, jsonify, request

# =========================
# CONFIG
# =========================
TZ = pytz.timezone("Asia/Jerusalem")

APP_MODE = os.getenv("APP_MODE", "real").lower().strip()  # "real" / "sim"
STATE_FILE = os.getenv("STATE_FILE", "state.json")

# Google Sheets
SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Time Tracking")
WORKSHEET_LOG = os.getenv("WORKSHEET_LOG", "Log")         # Daily summary + start-time cells
WORKSHEET_HOURLY = os.getenv("WORKSHEET_HOURLY", "Hourly")  # Hourly rows

# Log sheet layout (simple, stable):
# Row 1: headers (we set if empty)
# Columns: A Date, B Timer1, C Timer2, D ActivityTotal
# Start time row: B4 Timer1Start, C4 Timer2Start
LOG_HEADER_ROW = 1
LOG_START_TIME_ROW = 4
LOG_DATE_COL = 1
LOG_T1_COL = 2
LOG_T2_COL = 3
LOG_ACTIVITY_COL = 4

# Hourly sheet layout:
# Columns: A Date (YYYY-MM-DD), B Hour (0-23), C Timer1, D Timer2, E ActivityTotal
H_COL_DATE = 1
H_COL_HOUR = 2
H_COL_T1 = 3
H_COL_T2 = 4
H_COL_ACTIVITY = 5

# Auto-log hours
AUTO_LOG_HOURS = set(range(8, 24))  # 08–23
RESET_TIME_HHMM = (5, 0)            # daily reset at 05:00
CALENDAR_AUTO_TIME_HHMM = (0, 30)   # calendar auto update at 00:30

# =========================
# GOOGLE (Sheets / Calendar)
# =========================
def _load_sa_info(env_key: str) -> dict | None:
    raw = os.getenv(env_key)
    if not raw:
        return None
    return json.loads(raw)

def gs_connect():
    """
    Returns (ws_log, ws_hourly) or (None, None) in sim mode.
    """
    if APP_MODE == "sim":
        return None, None

    import gspread
    from google.oauth2.service_account import Credentials

    info = _load_sa_info("GOOGLE_CREDS_JSON")
    if not info:
        raise RuntimeError("Missing env GOOGLE_CREDS_JSON")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    gc = gspread.authorize(creds)

    sh = gc.open(SPREADSHEET_NAME)
    ws_log = sh.worksheet(WORKSHEET_LOG)

    try:
        ws_hourly = sh.worksheet(WORKSHEET_HOURLY)
    except Exception:
        ws_hourly = sh.add_worksheet(title=WORKSHEET_HOURLY, rows=2000, cols=10)

    _ensure_log_headers(ws_log)
    _ensure_hourly_headers(ws_hourly)
    return ws_log, ws_hourly

def _ensure_log_headers(ws):
    # Set headers if row1 is empty-ish
    vals = ws.row_values(LOG_HEADER_ROW)
    if len(vals) >= 4 and any(v.strip() for v in vals[:4]):
        return
    ws.update(f"A{LOG_HEADER_ROW}:D{LOG_HEADER_ROW}", [[
        "Date (YYYY-MM-DD)", "Timer1 (HH:MM:SS)", "Timer2 (HH:MM:SS)", "Activity (HH:MM:SS)"
    ]])
    ws.update(f"A{LOG_START_TIME_ROW}:C{LOG_START_TIME_ROW}", [[
        "Start Times", "Timer1 Start", "Timer2 Start"
    ]])

def _ensure_hourly_headers(ws):
    vals = ws.row_values(1)
    if len(vals) >= 5 and any(v.strip() for v in vals[:5]):
        return
    ws.update("A1:E1", [[
        "Date (YYYY-MM-DD)", "Hour", "Timer1 (HH:MM:SS)", "Timer2 (HH:MM:SS)", "Activity (HH:MM:SS)"
    ]])

def get_calendar_service():
    """
    Uses GOOGLE_CALENDAR_CREDS_JSON if provided, else falls back to GOOGLE_CREDS_JSON.
    """
    if APP_MODE == "sim":
        return None

    from googleapiclient.discovery import build
    from google.oauth2.service_account import Credentials

    info = _load_sa_info("GOOGLE_CALENDAR_CREDS_JSON") or _load_sa_info("GOOGLE_CREDS_JSON")
    if not info:
        raise RuntimeError("Missing env GOOGLE_CALENDAR_CREDS_JSON or GOOGLE_CREDS_JSON")

    scopes = ["https://www.googleapis.com/auth/calendar"]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return build("calendar", "v3", credentials=creds)

def update_calendar_daily_summary(calendar_id: str, day_: date, activity_time: str) -> bool:
    """
    Updates event with summary == 'סיכום יום' for the given day.
    """
    try:
        if APP_MODE == "sim":
            return True

        service = get_calendar_service()
        if service is None:
            return False

        start = TZ.localize(datetime.combine(day_, datetime.min.time()))
        end = start + timedelta(days=1)

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
                        break

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

    except Exception as e:
        print("Calendar update failed:", e)
        return False

# =========================
# TIME HELPERS
# =========================
def now_local() -> datetime:
    return datetime.now(TZ)

def sec_to_hms(total_seconds: int) -> str:
    if total_seconds < 0:
        total_seconds = 0
    h = total_seconds // 3600
    m = (total_seconds % 3600) // 60
    s = total_seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"

def hms_to_sec(hms: str) -> int:
    parts = hms.strip().split(":")
    if len(parts) != 3:
        raise ValueError("Expected HH:MM:SS")
    h, m, s = [int(x) for x in parts]
    return h * 3600 + m * 60 + s

# =========================
# STATE
# =========================
@dataclass
class TimerState:
    running: bool = False
    started_at_iso: str | None = None  # tz-aware iso
    accumulated_seconds: int = 0       # counted seconds (paused total)

@dataclass
class AppState:
    timer1: TimerState = TimerState()
    timer2: TimerState = TimerState()

    # daily activity accumulator (sum of both timers)
    activity_day: str = ""  # YYYY-MM-DD for which activity_seconds belongs
    activity_seconds: int = 0

    # for ensuring one run per scheduled moment
    last_hourly_log_key: str = ""       # e.g. "2026-01-06-14"
    last_daily_reset_key: str = ""      # e.g. "2026-01-06"
    last_calendar_update_key: str = ""  # e.g. "2026-01-06"

def default_state() -> AppState:
    d = now_local().date().isoformat()
    return AppState(
        timer1=TimerState(),
        timer2=TimerState(),
        activity_day=d,
        activity_seconds=0,
        last_hourly_log_key="",
        last_daily_reset_key="",
        last_calendar_update_key=""
    )

STATE_LOCK = threading.Lock()
STATE = default_state()

def load_state():
    global STATE
    if not os.path.exists(STATE_FILE):
        return
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)

        def _timer_from(dct):
            return TimerState(
                running=bool(dct.get("running", False)),
                started_at_iso=dct.get("started_at_iso"),
                accumulated_seconds=int(dct.get("accumulated_seconds", 0)),
            )

        STATE = AppState(
            timer1=_timer_from(raw.get("timer1", {})),
            timer2=_timer_from(raw.get("timer2", {})),
            activity_day=str(raw.get("activity_day", now_local().date().isoformat())),
            activity_seconds=int(raw.get("activity_seconds", 0)),
            last_hourly_log_key=str(raw.get("last_hourly_log_key", "")),
            last_daily_reset_key=str(raw.get("last_daily_reset_key", "")),
            last_calendar_update_key=str(raw.get("last_calendar_update_key", "")),
        )
    except Exception as e:
        print("Failed to load state:", e)

def save_state():
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(asdict(STATE), f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("Failed to save state:", e)

def _timer_ref(timer_id: int) -> TimerState:
    if timer_id == 1:
        return STATE.timer1
    if timer_id == 2:
        return STATE.timer2
    raise ValueError("timer_id must be 1 or 2")

def _timer_now_seconds(t: TimerState) -> int:
    sec = t.accumulated_seconds
    if t.running and t.started_at_iso:
        started = datetime.fromisoformat(t.started_at_iso)
        sec += int((now_local() - started).total_seconds())
    return max(sec, 0)

def _ensure_activity_day():
    """
    Daily tracking day follows local date. If day changed, roll.
    """
    today = now_local().date().isoformat()
    if STATE.activity_day != today:
        STATE.activity_day = today
        STATE.activity_seconds = 0

def _recalc_activity_seconds():
    """
    "Daily activity" = timer1 + timer2 seconds for current day.
    We keep it simple: just sum current timer totals.
    """
    _ensure_activity_day()
    STATE.activity_seconds = _timer_now_seconds(STATE.timer1) + _timer_now_seconds(STATE.timer2)

# =========================
# SHEET WRITERS
# =========================
def _find_or_append_date_row(ws_log, day_iso: str) -> int:
    """
    Finds date in col A. If not found, appends.
    """
    col = ws_log.col_values(LOG_DATE_COL)
    # search in existing
    for idx, v in enumerate(col, start=1):
        if v.strip() == day_iso:
            return idx
    # append
    next_row = len(col) + 1
    ws_log.update_cell(next_row, LOG_DATE_COL, day_iso)
    return next_row

def sheet_write_daily(ws_log):
    """
    Writes daily totals into Log sheet for today's date.
    """
    _recalc_activity_seconds()
    day_iso = now_local().date().isoformat()
    t1 = sec_to_hms(_timer_now_seconds(STATE.timer1))
    t2 = sec_to_hms(_timer_now_seconds(STATE.timer2))
    act = sec_to_hms(STATE.activity_seconds)

    if APP_MODE == "sim":
        return True

    row = _find_or_append_date_row(ws_log, day_iso)
    ws_log.update(f"B{row}:D{row}", [[t1, t2, act]])
    return True

def sheet_write_start_time(ws_log, timer_id: int, started_dt: datetime):
    """
    Writes start time to row 4 (B4/C4).
    """
    if APP_MODE == "sim":
        return True
    col = LOG_T1_COL if timer_id == 1 else LOG_T2_COL
    ws_log.update_cell(LOG_START_TIME_ROW, col, started_dt.strftime("%H:%M:%S"))
    return True

def sheet_write_hourly(ws_hourly, target_date_iso: str, target_hour: int):
    """
    Writes a row into Hourly sheet (upsert by Date+Hour).
    """
    _recalc_activity_seconds()
    t1 = sec_to_hms(_timer_now_seconds(STATE.timer1))
    t2 = sec_to_hms(_timer_now_seconds(STATE.timer2))
    act = sec_to_hms(STATE.activity_seconds)

    if APP_MODE == "sim":
        return True

    # find row with matching date+hour
    all_dates = ws_hourly.col_values(H_COL_DATE)
    all_hours = ws_hourly.col_values(H_COL_HOUR)

    row = None
    for i in range(2, len(all_dates) + 1):  # from row2
        if all_dates[i-1].strip() == target_date_iso and str(all_hours[i-1]).strip() == str(target_hour):
            row = i
            break

    if row is None:
        row = len(all_dates) + 1
        ws_hourly.update(f"A{row}:E{row}", [[target_date_iso, str(target_hour), t1, t2, act]])
    else:
        ws_hourly.update(f"C{row}:E{row}", [[t1, t2, act]])

    return True

# =========================
# SCHEDULER
# =========================
def _maybe_hourly_log(ws_hourly):
    n = now_local()

    # hourly log only once per hour:
    # - between 08-23: write for that hour when minute==0..1
    # - at midnight 00:00..00:01: write to 23 of previous day
    if n.minute not in (0, 1):
        return

    if n.hour in AUTO_LOG_HOURS:
        target_date_iso = n.date().isoformat()
        target_hour = n.hour
    elif n.hour == 0:
        target_date_iso = (n.date() - timedelta(days=1)).isoformat()
        target_hour = 23
    else:
        return

    key = f"{n.date().isoformat()}-{n.hour}"
    if STATE.last_hourly_log_key == key:
        return

    ok = sheet_write_hourly(ws_hourly, target_date_iso, target_hour)
    if ok:
        STATE.last_hourly_log_key = key
        save_state()

def _maybe_daily_reset(ws_log, ws_hourly):
    n = now_local()
    hh, mm = RESET_TIME_HHMM
    if not (n.hour == hh and n.minute in (0, 1)):
        return

    key = n.date().isoformat()
    if STATE.last_daily_reset_key == key:
        return

    # reset timers
    STATE.timer1 = TimerState()
    STATE.timer2 = TimerState()
    STATE.activity_day = n.date().isoformat()
    STATE.activity_seconds = 0
    STATE.last_daily_reset_key = key

    # write reset snapshot (optional)
    try:
        if ws_log:
            sheet_write_daily(ws_log)
    except Exception as e:
        print("Daily reset sheet write failed:", e)

    save_state()

def _maybe_calendar_auto_update():
    n = now_local()
    hh, mm = CALENDAR_AUTO_TIME_HHMM
    if not (n.hour == hh and n.minute in (mm, mm+1)):
        return

    key = n.date().isoformat()
    if STATE.last_calendar_update_key == key:
        return

    calendar_id = os.getenv("CALENDAR_ID", "").strip()
    if not calendar_id:
        # if no calendar set, just mark as done to prevent spam
        STATE.last_calendar_update_key = key
        save_state()
        return

    _recalc_activity_seconds()
    act = sec_to_hms(STATE.activity_seconds)

    ok = update_calendar_daily_summary(calendar_id=calendar_id, day_=n.date(), activity_time=act)
    if ok:
        STATE.last_calendar_update_key = key
        save_state()

def scheduler_loop():
    """
    Background scheduler:
    - auto hourly log (08–23 + midnight->23 yesterday)
    - daily reset 05:00
    - calendar auto update 00:30
    """
    print("Scheduler started. mode=", APP_MODE)
    ws_log = None
    ws_hourly = None

    while True:
        try:
            with STATE_LOCK:
                # connect (real) lazily
                if APP_MODE != "sim" and (ws_log is None or ws_hourly is None):
                    ws_log, ws_hourly = gs_connect()

                # do scheduled checks
                if ws_hourly is not None or APP_MODE == "sim":
                    _maybe_hourly_log(ws_hourly)
                if ws_log is not None or APP_MODE == "sim":
                    _maybe_daily_reset(ws_log, ws_hourly)
                _maybe_calendar_auto_update()

        except Exception as e:
            print("Scheduler tick error:", e)

        time.sleep(20)

# =========================
# FLASK
# =========================
app = Flask(__name__)

@app.get("/api/ping")
def ping():
    return jsonify({"ok": True, "mode": APP_MODE, "ts": now_local().isoformat()})

@app.get("/api/status")
def status():
    with STATE_LOCK:
        _recalc_activity_seconds()
        return jsonify({
            "mode": APP_MODE,
            "now": now_local().isoformat(),
            "timer1": {
                "running": STATE.timer1.running,
                "seconds": _timer_now_seconds(STATE.timer1),
                "hms": sec_to_hms(_timer_now_seconds(STATE.timer1)),
            },
            "timer2": {
                "running": STATE.timer2.running,
                "seconds": _timer_now_seconds(STATE.timer2),
                "hms": sec_to_hms(_timer_now_seconds(STATE.timer2)),
            },
            "activity": {
                "day": STATE.activity_day,
                "seconds": STATE.activity_seconds,
                "hms": sec_to_hms(STATE.activity_seconds),
            },
            "scheduler": {
                "last_hourly_log_key": STATE.last_hourly_log_key,
                "last_daily_reset_key": STATE.last_daily_reset_key,
                "last_calendar_update_key": STATE.last_calendar_update_key,
            }
        })

def _ensure_ws():
    if APP_MODE == "sim":
        return None, None
    return gs_connect()

def _timer_start(timer_id: int):
    t = _timer_ref(timer_id)
    if t.running:
        return
    t.running = True
    t.started_at_iso = now_local().isoformat()

    # write start time to row 4 (B4/C4)
    try:
        ws_log, _ = _ensure_ws()
        if ws_log is not None:
            sheet_write_start_time(ws_log, timer_id, now_local())
    except Exception as e:
        print("Start time write failed:", e)

def _timer_stop(timer_id: int):
    t = _timer_ref(timer_id)
    if not t.running:
        return
    started = datetime.fromisoformat(t.started_at_iso) if t.started_at_iso else None
    if started:
        t.accumulated_seconds += int((now_local() - started).total_seconds())
    t.running = False
    t.started_at_iso = None

def _timer_reset(timer_id: int):
    t = _timer_ref(timer_id)
    t.running = False
    t.started_at_iso = None
    t.accumulated_seconds = 0

def _timer_adjust(timer_id: int, delta_seconds: int):
    t = _timer_ref(timer_id)
    # adjust accumulated only (doesn't change running start time)
    t.accumulated_seconds = max(0, t.accumulated_seconds + delta_seconds)

def _timer_set(timer_id: int, seconds: int):
    t = _timer_ref(timer_id)
    t.accumulated_seconds = max(0, int(seconds))
    # keep running state; if running, we reset started_at so total stays stable
    if t.running:
        t.started_at_iso = now_local().isoformat()

@app.post("/api/timer/<int:timer_id>/start")
def api_start(timer_id: int):
    with STATE_LOCK:
        _timer_start(timer_id)
        save_state()
    return jsonify({"ok": True})

@app.post("/api/timer/<int:timer_id>/stop")
def api_stop(timer_id: int):
    with STATE_LOCK:
        _timer_stop(timer_id)
        save_state()
    return jsonify({"ok": True})

@app.post("/api/timer/<int:timer_id>/reset")
def api_reset(timer_id: int):
    with STATE_LOCK:
        _timer_reset(timer_id)
        save_state()
    return jsonify({"ok": True})

@app.post("/api/timer/<int:timer_id>/plus5")
def api_plus5(timer_id: int):
    with STATE_LOCK:
        _timer_adjust(timer_id, 5 * 60)
        save_state()
    return jsonify({"ok": True})

@app.post("/api/timer/<int:timer_id>/minus10")
def api_minus10(timer_id: int):
    with STATE_LOCK:
        _timer_adjust(timer_id, -10 * 60)
        save_state()
    return jsonify({"ok": True})

@app.post("/api/timer/<int:timer_id>/set")
def api_set(timer_id: int):
    data = request.get_json(silent=True) or {}
    value = (data.get("value") or "").strip()
    # accept "HH:MM:SS" or seconds int
    with STATE_LOCK:
        try:
            if ":" in value:
                seconds = hms_to_sec(value)
            else:
                seconds = int(value)
            _timer_set(timer_id, seconds)
            save_state()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400

# ✅ Manual write to Sheet
@app.post("/api/sheet/write")
def api_sheet_write():
    with STATE_LOCK:
        try:
            ws_log, _ = _ensure_ws()
            if ws_log is None and APP_MODE != "sim":
                return jsonify({"ok": False, "error": "Sheets not connected"}), 500
            ok = sheet_write_daily(ws_log)
            save_state()
            return jsonify({"ok": ok})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

# ✅ Manual calendar update
@app.post("/api/calendar/update")
def api_calendar_update():
    data = request.get_json(silent=True) or {}
    calendar_id = (data.get("calendar_id") or os.getenv("CALENDAR_ID", "")).strip()
    if not calendar_id:
        return jsonify({"ok": False, "error": "Missing calendar_id (set CALENDAR_ID or pass in body)"}), 400

    with STATE_LOCK:
        _recalc_activity_seconds()
        act = sec_to_hms(STATE.activity_seconds)
        ok = update_calendar_daily_summary(calendar_id=calendar_id, day_=now_local().date(), activity_time=act)
        return jsonify({"ok": ok, "activity": act})

# ✅ Endpoints for testing/debug
@app.get("/api/debug/state")
def api_debug_state():
    with STATE_LOCK:
        return jsonify(asdict(STATE))

@app.get("/api/debug/keys")
def api_debug_keys():
    """
    Quick check for key hooks: returns what the server "thinks" time windows are.
    """
    n = now_local()
    return jsonify({
        "now": n.isoformat(),
        "hour": n.hour,
        "minute": n.minute,
        "auto_log_hours": sorted(list(AUTO_LOG_HOURS)),
        "reset_at": f"{RESET_TIME_HHMM[0]:02d}:{RESET_TIME_HHMM[1]:02d}",
        "calendar_auto_at": f"{CALENDAR_AUTO_TIME_HHMM[0]:02d}:{CALENDAR_AUTO_TIME_HHMM[1]:02d}",
    })

# =========================
# BOOT
# =========================
def start_background():
    t = threading.Thread(target=scheduler_loop, daemon=True)
    t.start()

load_state()
start_background()

if __name__ == "__main__":
    # local dev
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)