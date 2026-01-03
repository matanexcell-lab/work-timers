import os
import json
import sqlite3
import re
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
LAST_LOG_HOUR = 24  # "24" אצלנו ממופה לשורה של 23:00

SPREADSHEET_NAME = "Time Tracking"
WORKSHEET_NAME = "Log"

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
        running INTEGER NOT NULL DEFAULT 0,  -- 0/1
        elapsed INTEGER NOT NULL DEFAULT 0,  -- seconds accumulated (when not running)
        start_epoch REAL,                   -- unix epoch when started (real mode)
        start_sim_iso TEXT,                 -- iso datetime when started (sim mode, TZ-local, no offset)
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

    # Ensure meta keys exist
    defaults = {
        "sim_enabled": "0",
        "sim_now_iso": "",

        # Reset tracking per mode
        "last_reset_date_real": "",
        "last_reset_date_sim": "",

        # Auto log slot per mode (so we don't miss hours and don't duplicate)
        "last_auto_slot_real": "",
        "last_auto_slot_sim": "",

        # Manual log status (for UI)
        "last_log_time": "",
        "last_log_ok": "",
        "last_log_msg": "",

        # Start-time per mode+timer (per sheet day)
        "start_logged_day_real_1": "",
        "start_logged_day_real_2": "",
        "start_logged_day_sim_1": "",
        "start_logged_day_sim_2": "",
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

def fmt(sec: int) -> str:
    sec = max(0, int(sec))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

# =========================
# RESET (05:00) per mode
# =========================
def ensure_daily_reset_for_mode(mode: str, clock_dt: datetime):
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

    # allow start-time to be logged again for this day
    for i in range(1, TIMER_COUNT + 1):
        set_meta(f"start_logged_day_{mode}_{i}", "")

# =========================
# TIMER CALC
# =========================
def timer_total_seconds(mode: str, timer_id: int, clock_dt: datetime) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, timer_id))
    t = cur.fetchone()
    conn.close()

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
    Rule:
    - 00:00–07:59 -> write to 23 of previous day
    - hour > 23 -> clamp to 23
    - else -> use clock_dt.hour
    """
    if clock_dt.hour < FIRST_LOG_HOUR:
        return 23, (clock_dt.date() - timedelta(days=1))
    if clock_dt.hour > 23:
        return 23, clock_dt.date()
    return clock_dt.hour, clock_dt.date()

def sheet_cell_for_hour_day(ws, hour: int, day):
    """
    row 7 = 08:00, row 22 = 23:00
    """
    headers = ws.row_values(3)
    date_str = day.strftime("%d/%m/%Y")
    if date_str not in headers:
        return None, None, f"תאריך {date_str} לא נמצא בשורה 3"

    col = headers.index(date_str) + 1
    row = 7 + (hour - 8)
    if row < 7:
        row = 7
    if row > 22:
        row = 22
    return row, col, ""

def build_two_timers_value(mode: str, clock_dt: datetime) -> str:
    t1 = fmt(timer_total_seconds(mode, 1, clock_dt))
    t2 = fmt(timer_total_seconds(mode, 2, clock_dt))
    return f"T1 {t1} | T2 {t2}"

def log_to_sheet_for_clock(mode: str, clock_dt: datetime, force: bool, reason: str):
    """
    Writes BOTH timers for the given mode (real/sim) into the sheet cell matching clock_dt.
    Returns (ok, msg).
    """
    ws = gs_connect()
    if ws is None:
        return False, "חסרים Google credentials (GOOGLE_CREDS_JSON)"

    hour, day = target_hour_and_date(clock_dt)
    row, col, err = sheet_cell_for_hour_day(ws, hour, day)
    if err:
        return False, err

    value = build_two_timers_value(mode, clock_dt)
    ws.update_cell(row, col, value)
    return True, f"נשלח עדכון ({reason}) לתא {row},{col}"

# =========================
# START TIME (Row 4) per timer
# =========================
def update_start_time_cell(mode: str, clock_dt: datetime, timer_id: int):
    """
    Write "T1 HH:MM | T2 HH:MM" into row 4 for the sheet day of clock_dt,
    first Start after 05:00 per timer.
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

    meta_key = f"start_logged_day_{mode}_{timer_id}"
    if get_meta(meta_key) == day_key:
        return

    headers = ws.row_values(3)
    date_str = day.strftime("%d/%m/%Y")
    if date_str not in headers:
        return

    col = headers.index(date_str) + 1

    # read current cell, then merge per timer
    current = ws.cell(4, col).value or ""
    current = str(current).strip()

    # parse existing like "T1 09:10 | T2 10:05"
    def set_part(text: str, tid: int, hhmm: str):
        pattern = rf"(T{tid}\s+\d{{2}}:\d{{2}})"
        if re.search(pattern, text):
            return re.sub(pattern, f"T{tid} {hhmm}", text)
        # if missing, append nicely
        if text:
            return f"{text} | T{tid} {hhmm}"
        return f"T{tid} {hhmm}"

    new_text = set_part(current, timer_id, clock_dt.strftime("%H:%M"))

    # also normalize order if we want T1 first then T2 (optional, safe)
    # keep minimal: just write merged string as built
    ws.update_cell(4, col, new_text)
    set_meta(meta_key, day_key)

# =========================
# AUTO LOG (not missing hour) per mode
# =========================
def maybe_auto_log_for_mode(mode: str, clock_dt: datetime):
    """
    Auto log once per hour-slot between 08..23 (and "24" is handled by clamping to 23 if hour>23).
    IMPORTANT: Not missing even if request arrives at :38.
    We log the first time we notice a new slot.
    """
    if clock_dt is None:
        return

    # Only auto between 08..23 (24 doesn't exist; clamp handled in target_hour_and_date if hour>23)
    if clock_dt.hour < FIRST_LOG_HOUR:
        return
    if clock_dt.hour > 23:
        # hour>23 treated as 23 slot, but auto-log rule says 08-24,
        # so we accept and treat it as 23 slot.
        pass

    hour, day = target_hour_and_date(clock_dt)
    slot = f"{day.isoformat()}-{hour:02d}"

    meta_key = f"last_auto_slot_{mode}"
    last_slot = get_meta(meta_key)
    if last_slot == slot:
        return

    ok, msg = log_to_sheet_for_clock(mode, clock_dt, force=False, reason="אוטומטי")
    if ok:
        set_meta(meta_key, slot)
        set_meta("last_log_time", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))
        set_meta("last_log_ok", "1")
        set_meta("last_log_msg", "אוטומטי: " + msg)

# =========================
# TIME PARSE (Set)
# =========================
def parse_time_to_seconds(s: str) -> int | None:
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None

    # allow HH, MM:SS, HH:MM:SS
    if re.fullmatch(r"\d{1,3}", s):
        # interpret as hours
        return int(s) * 3600

    if re.fullmatch(r"\d{1,2}:\d{2}", s):
        mm, ss = s.split(":")
        return int(mm) * 60 + int(ss)

    if re.fullmatch(r"\d{1,2}:\d{2}:\d{2}", s):
        hh, mm, ss = s.split(":")
        return int(hh) * 3600 + int(mm) * 60 + int(ss)

    return None

# =========================
# ROUTES
# =========================
@app.route("/")
@app.route("/ui")
def ui():
    return render_template("index.html")

@app.route("/api/status")
def status():
    # get current clock
    clock = now()
    if clock is None:
        set_meta("sim_enabled", "0")
        clock = tz_now_real()

    # advance sim by 1 sec per status call
    if sim_enabled():
        set_sim_now(clock + timedelta(seconds=1))
        clock = get_sim_now()

    # reset logic per mode (based on each clock)
    ensure_daily_reset_for_mode("real", tz_now_real())
    ensure_daily_reset_for_mode("sim", clock if sim_enabled() else None)

    # auto log for both modes:
    # - real based on real clock
    # - sim based on sim clock (only if enabled)
    maybe_auto_log_for_mode("real", tz_now_real())
    if sim_enabled():
        maybe_auto_log_for_mode("sim", clock)

    mode = current_mode()
    timers = [fmt(timer_total_seconds(mode, i, clock)) for i in range(1, TIMER_COUNT + 1)]

    return jsonify({
        "now_str": clock.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled(),
        "timers": timers,
        "last_log": {
            "time": get_meta("last_log_time") or "—",
            "ok": (get_meta("last_log_ok") == "1"),
            "msg": get_meta("last_log_msg") or ""
        }
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock = now() or tz_now_real()

    # ✅ Start time per timer (real+sim)
    update_start_time_cell(mode, clock, i)

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
            """, (clock.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))

    conn.commit()
    conn.close()
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock = now() or tz_now_real()

    total = timer_total_seconds(mode, i, clock)

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
    return jsonify(ok=True)

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
    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/adjust", methods=["POST"])
def adjust_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    data = request.get_json(force=True)
    delta = int(data.get("delta", 0))

    mode = current_mode()
    clock = now() or tz_now_real()

    # current total
    current = timer_total_seconds(mode, i, clock)
    new_val = max(0, current + delta)

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    if t is None:
        conn.close()
        return jsonify(error="timer missing"), 500

    running = int(t["running"]) == 1

    if running:
        # keep running: set elapsed to new_val, reset start to "now"
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
            """, (new_val, clock.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))
    else:
        # not running
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
    val = data.get("value", "")
    seconds = parse_time_to_seconds(val)
    if seconds is None:
        return jsonify(error="פורמט לא תקין. השתמש: HH:MM:SS או MM:SS או HH"), 400

    mode = current_mode()
    clock = now() or tz_now_real()

    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM timers WHERE mode=? AND timer_id=?", (mode, i))
    t = cur.fetchone()
    if t is None:
        conn.close()
        return jsonify(error="timer missing"), 500

    running = int(t["running"]) == 1

    if running:
        # keep running: elapsed becomes seconds; restart from now
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
            """, (seconds, clock.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))
    else:
        cur.execute("""
        UPDATE timers
        SET elapsed=?, start_epoch=NULL, start_sim_iso=NULL
        WHERE mode=? AND timer_id=?
        """, (seconds, mode, i))

    conn.commit()
    conn.close()
    return jsonify(ok=True, new_time=fmt(seconds))

@app.route("/api/log-now", methods=["POST"])
def manual_log():
    """
    Manual log uses CURRENT MODE + CURRENT CLOCK.
    Writes both timers, and returns explicit ok/message + updates UI meta.
    """
    mode = current_mode()
    clock = now() or tz_now_real()

    try:
        ok, msg = log_to_sheet_for_clock(mode, clock, force=True, reason="ידני")
        set_meta("last_log_time", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))
        set_meta("last_log_ok", "1" if ok else "0")
        set_meta("last_log_msg", msg)
        return jsonify(ok=ok, message=msg), (200 if ok else 500)
    except Exception as e:
        set_meta("last_log_time", tz_now_real().strftime("%d/%m/%Y %H:%M:%S"))
        set_meta("last_log_ok", "0")
        set_meta("last_log_msg", str(e))
        return jsonify(ok=False, message=str(e)), 500

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