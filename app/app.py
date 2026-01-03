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
LAST_LOG_HOUR = 24  # "24" maps to last row/23

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

# =========================
# SQLITE (Shared across workers)
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
        running INTEGER NOT NULL DEFAULT 0,  -- 0/1
        elapsed INTEGER NOT NULL DEFAULT 0,  -- seconds accumulated baseline
        start_epoch REAL,                   -- unix epoch when started (real mode)
        start_sim_iso TEXT,                 -- iso datetime when started (sim mode)
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
            INSERT OR IGNORE INTO timers(mode, timer_id, running, elapsed, start_epoch, start_sim_iso)
            VALUES (?, ?, 0, 0, NULL, NULL)
            """, (mode, i))

    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",

        # resets per mode
        "last_reset_date_real": "",
        "last_reset_date_sim": "",

        # hourly logging state per mode (so we don't "miss hour" or repeat)
        "last_logged_hour_real": "",
        "last_logged_day_real": "",
        "last_logged_hour_sim": "",
        "last_logged_day_sim": "",

        # last sheet update time (for UI)
        "last_sheet_update_real": "",
        "last_sheet_update_sim": "",

        # start time written per mode (row 4)
        "first_start_logged_day_real": "",
        "first_start_logged_day_sim": "",
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
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now():
    return get_sim_now() if sim_enabled() else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# RESET LOGIC (05:00) - per mode
# =========================
def ensure_daily_reset_for_mode(mode: str, clock_dt: datetime):
    """
    Reset timers for that mode at 05:00 (once per day per mode).
    """
    if clock_dt is None:
        return
    if clock_dt.hour < RESET_HOUR:
        return

    key = f"last_reset_date_{mode}"
    last = get_meta(key)  # YYYY-MM-DD
    today = clock_dt.date().isoformat()
    if last == today:
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

    set_meta(key, today)

    # allow start-time write again for this sheet-day
    set_meta(f"first_start_logged_day_{mode}", "")

# =========================
# TIMER CALCULATION
# =========================
def timer_total_seconds(mode: str, timer_id: int, clock_dt: datetime) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, timer_id))
    t = cur.fetchone()
    conn.close()

    elapsed = int(t["elapsed"])
    running = int(t["running"]) == 1

    if not running:
        return elapsed

    if mode == "real":
        if t["start_epoch"] is None:
            return elapsed
        now_epoch = tz_now_real().timestamp()
        return elapsed + int(max(0, now_epoch - float(t["start_epoch"])))

    # sim mode: use clock_dt
    if t["start_sim_iso"] is None or clock_dt is None:
        return elapsed

    start_dt = datetime.fromisoformat(t["start_sim_iso"])
    if start_dt.tzinfo is None:
        start_dt = TZ.localize(start_dt)
    diff = int(max(0, (clock_dt - start_dt).total_seconds()))
    return elapsed + diff

def fmt(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

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

def target_hour_and_date(clock_dt: datetime):
    """
    Rules (as requested):
    - If 00:00–07:59 -> write to 23 of previous day
    - If hour > 23 -> clamp to 23 (your "24" maps to last row)
    - Else use current hour
    """
    if clock_dt.hour < FIRST_LOG_HOUR:
        return 23, (clock_dt.date() - timedelta(days=1))
    if clock_dt.hour > 23:
        return 23, clock_dt.date()
    return clock_dt.hour, clock_dt.date()

def _row_for_hour(hour: int) -> int:
    # sheet rows: row 7 = 08:00, row 22 = 23:00
    row = 7 + (hour - 8)
    if row < 7:
        row = 7
    if row > 22:
        row = 22
    return row

def log_to_sheet(mode: str, force: bool, clock_dt: datetime):
    ws = gs_connect()
    if ws is None or clock_dt is None:
        return False, "Google Sheet לא זמין", ""

    hour, day = target_hour_and_date(clock_dt)

    last_h = get_meta(f"last_logged_hour_{mode}")
    last_d = get_meta(f"last_logged_day_{mode}")
    day_key = day.isoformat()

    if not force and last_h == str(hour) and last_d == day_key:
        return True, "already logged", get_meta(f"last_sheet_update_{mode}")

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)

    if date_str not in headers:
        return False, f"תאריך {date_str} לא נמצא", ""

    base_col = headers.index(date_str) + 1
    row = _row_for_hour(hour)

    # ⏱ חישוב זמנים
    t1 = fmt(timer_total_seconds(mode, 1, clock_dt))
    t2 = fmt(timer_total_seconds(mode, 2, clock_dt))

    # ✅ כתיבה לשתי עמודות נפרדות
    ws.update_cell(row, base_col, t1)        # Timer 1
    ws.update_cell(row, base_col + 1, t2)    # Timer 2

    set_meta(f"last_logged_hour_{mode}", str(hour))
    set_meta(f"last_logged_day_{mode}", day_key)

    updated_at = tz_now_real().strftime("%d/%m/%Y %H:%M:%S")
    set_meta(f"last_sheet_update_{mode}", updated_at)

    return True, "logged", updated_at

def maybe_auto_log(mode: str, clock_dt: datetime):
    """
    FIX: no longer requires hitting exactly :00:00.
    Instead: if we're in target window (08–24) and we haven't logged this target hour/day yet,
    log on the *first* status request within that hour (even at :38).
    """
    if clock_dt is None:
        return

    # Determine which "hour/day" we should log for
    hour, day = target_hour_and_date(clock_dt)

    # Only auto log for working hours 08..24 (24 treated as 23)
    # If after midnight mapped to 23 prev day -> still allowed (it's within 08..24 window for logging),
    # but you mainly care about work hours.
    if not (FIRST_LOG_HOUR <= hour <= 23):
        return

    # Use existing de-dup keys
    last_h = get_meta(f"last_logged_hour_{mode}")
    last_d = get_meta(f"last_logged_day_{mode}")
    day_key = day.isoformat()

    if last_h == str(hour) and last_d == day_key:
        return

    log_to_sheet(mode=mode, force=False, clock_dt=clock_dt)

# =========================
# START TIME LOG (Row 4) - per mode
# =========================
def log_start_time_if_needed(mode: str, clock_dt: datetime):
    """
    Write start time (HH:MM) to row 4 for the sheet date of clock_dt,
    on first Start after 05:00 (per mode).
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

    meta_key = f"first_start_logged_day_{mode}"
    if get_meta(meta_key) == day_key:
        return

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1
    ws.update_cell(4, col, clock_dt.strftime("%H:%M"))
    set_meta(meta_key, day_key)

# =========================
# ROUTES
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    mode = current_mode()
    clock_dt = now()

    if clock_dt is None:
        set_meta("sim_enabled", "0")
        clock_dt = tz_now_real()
        mode = "real"

    # advance sim clock by 1 sec (client calls status every second)
    if sim_enabled():
        set_sim_now(clock_dt + timedelta(seconds=1))
        clock_dt = get_sim_now()

    # reset per mode using that mode's clock
    ensure_daily_reset_for_mode(mode, clock_dt)

    # auto log per mode using that mode's clock (FIXED not to miss hour)
    maybe_auto_log(mode, clock_dt)

    timers = [fmt(timer_total_seconds(mode, i, clock_dt)) for i in range(1, TIMER_COUNT + 1)]

    last_sheet = get_meta(f"last_sheet_update_{mode}")
    if not last_sheet:
        last_sheet = ""

    return jsonify({
        "now_str": clock_dt.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled(),
        "mode": mode,
        "timers": timers,
        "last_sheet_update": last_sheet
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock_dt = now() or tz_now_real()

    # start time write (per mode)
    log_start_time_if_needed(mode, clock_dt)

    conn = db()
    cur = conn.cursor()

    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    if t is None:
        conn.close()
        return jsonify(error="timer missing"), 500

    if int(t["running"]) == 0:
        if mode == "real":
            cur.execute("""
                UPDATE timers
                SET running=1, start_epoch=?
                WHERE mode=? AND timer_id=?
            """, (tz_now_real().timestamp(), mode, i))
        else:
            cur.execute("""
                UPDATE timers
                SET running=1, start_sim_iso=?
                WHERE mode=? AND timer_id=?
            """, (clock_dt.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))

    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock_dt = now() or tz_now_real()

    total = timer_total_seconds(mode, i, clock_dt)

    conn = db()
    cur = conn.cursor()
    if mode == "real":
        cur.execute("""
            UPDATE timers
            SET running=0, elapsed=?, start_epoch=NULL
            WHERE mode=? AND timer_id=?
        """, (total, mode, i))
    else:
        cur.execute("""
            UPDATE timers
            SET running=0, elapsed=?, start_sim_iso=NULL
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

# =========================
# TIMER EDIT: adjust (+/- seconds) + set absolute seconds
# =========================
@app.route("/api/timer/<int:i>/adjust", methods=["POST"])
def adjust_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    data = request.get_json(force=True)
    delta = int(data.get("delta", 0))

    mode = current_mode()
    clock_dt = now() or tz_now_real()

    # compute current
    current = timer_total_seconds(mode, i, clock_dt)
    new_val = max(0, current + delta)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    if t is None:
        conn.close()
        return jsonify(error="timer missing"), 500

    running = int(t["running"]) == 1

    # keep running state if it was running (so you can edit while running)
    if running:
        if mode == "real":
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_epoch=?
                WHERE mode=? AND timer_id=?
            """, (new_val, tz_now_real().timestamp(), mode, i))
        else:
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_sim_iso=?
                WHERE mode=? AND timer_id=?
            """, (new_val, clock_dt.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))
    else:
        cur.execute("""
            UPDATE timers
            SET elapsed=?, start_epoch=NULL, start_sim_iso=NULL
            WHERE mode=? AND timer_id=?
        """, (new_val, mode, i))

    conn.commit()
    conn.close()
    return jsonify(ok=True, new_time=fmt(new_val))

@app.route("/api/timer/<int:i>/set", methods=["POST"])
def set_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    data = request.get_json(force=True)
    seconds = int(data.get("seconds", -1))
    if seconds < 0:
        return jsonify(error="bad seconds"), 400

    mode = current_mode()
    clock_dt = now() or tz_now_real()

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    if t is None:
        conn.close()
        return jsonify(error="timer missing"), 500

    running = int(t["running"]) == 1

    # keep running state if it was running
    if running:
        if mode == "real":
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_epoch=?
                WHERE mode=? AND timer_id=?
            """, (seconds, tz_now_real().timestamp(), mode, i))
        else:
            cur.execute("""
                UPDATE timers
                SET elapsed=?, start_sim_iso=?
                WHERE mode=? AND timer_id=?
            """, (seconds, clock_dt.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))
    else:
        cur.execute("""
            UPDATE timers
            SET elapsed=?, start_epoch=NULL, start_sim_iso=NULL
            WHERE mode=? AND timer_id=?
        """, (seconds, mode, i))

    conn.commit()
    conn.close()
    return jsonify(ok=True, new_time=fmt(seconds))

# =========================
# Manual log (button)
# =========================
@app.route("/api/log-now", methods=["POST"])
def manual_log():
    mode = current_mode()
    clock_dt = now() or tz_now_real()
    ok, msg, updated_at = log_to_sheet(mode=mode, force=True, clock_dt=clock_dt)
    return jsonify(ok=ok, message=msg, updated_at=updated_at), (200 if ok else 500)

# =========================
# SIMULATION
# =========================
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

if __name__ == "__main__":
    app.run(debug=True)