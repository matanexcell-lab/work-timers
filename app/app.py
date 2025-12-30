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
LAST_LOG_HOUR = 24  # "24" maps to last row (23-24)

SPREADSHEET_NAME = os.getenv("SPREADSHEET_NAME", "Time Tracking")
WORKSHEET_NAME = os.getenv("WORKSHEET_NAME", "Log")

# Timer edit policy
ALLOW_EDIT_WHILE_RUNNING = True  # אתה רוצה "חופשי" => True

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
        start_sim_iso TEXT,                 -- iso datetime (no tz) when started (sim mode)
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

        # per-mode daily reset (YYYY-MM-DD)
        "last_reset_date_real": "",
        "last_reset_date_sim": "",

        # per-mode last logged hour/day
        "last_logged_hour_real": "",
        "last_logged_day_real": "",   # YYYY-MM-DD
        "last_logged_hour_sim": "",
        "last_logged_day_sim": "",

        # per-mode first start time written (row 4)
        "first_start_logged_day_real": "",  # YYYY-MM-DD
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
    # store without tzinfo as "wall clock"
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now():
    return get_sim_now() if sim_enabled() else tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# FORMAT
# =========================
def fmt(sec: int) -> str:
    if sec < 0:
        sec = 0
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02}:{m:02}:{s:02}"

def hms_to_seconds(hms: str) -> int:
    """
    Accepts:
      HH:MM:SS
      H:MM:SS
      MM:SS  (treated as 00:MM:SS)
    """
    parts = (hms or "").strip().split(":")
    if len(parts) == 2:
        h = 0
        m = int(parts[0])
        s = int(parts[1])
    elif len(parts) == 3:
        h = int(parts[0])
        m = int(parts[1])
        s = int(parts[2])
    else:
        raise ValueError("Bad time format")
    return max(0, h * 3600 + m * 60 + s)

# =========================
# RESET LOGIC (05:00) - per mode clock
# =========================
def ensure_daily_reset_for_mode(mode: str, clock_dt: datetime):
    """
    Reset that mode's timers at/after 05:00 once per day (by that mode clock).
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
    # allow start time logging again for that mode/day
    set_meta(f"first_start_logged_day_{mode}", "")

# =========================
# TIMER CALCULATION
# =========================
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

    # sim mode
    if t["start_sim_iso"] is None or clock_dt is None:
        return elapsed
    start_dt = datetime.fromisoformat(t["start_sim_iso"])
    if start_dt.tzinfo is None:
        start_dt = TZ.localize(start_dt)
    diff = int(max(0, (clock_dt - start_dt).total_seconds()))
    return elapsed + diff

def set_timer_total(mode: str, timer_id: int, new_total_sec: int, keep_running: bool, clock_dt: datetime):
    """
    Sets timer total to new_total_sec.
    If keep_running is True and timer was running, it remains running from "now".
    """
    new_total_sec = max(0, int(new_total_sec))

    t = timer_row(mode, timer_id)
    if t is None:
        return

    was_running = int(t["running"]) == 1
    running_after = (keep_running and was_running)

    conn = db()
    cur = conn.cursor()

    if mode == "real":
        if running_after:
            cur.execute("""
                UPDATE timers
                SET elapsed=?, running=1, start_epoch=?, start_sim_iso=NULL
                WHERE mode=? AND timer_id=?
            """, (new_total_sec, tz_now_real().timestamp(), mode, timer_id))
        else:
            cur.execute("""
                UPDATE timers
                SET elapsed=?, running=0, start_epoch=NULL, start_sim_iso=NULL
                WHERE mode=? AND timer_id=?
            """, (new_total_sec, mode, timer_id))
    else:
        if clock_dt is None:
            clock_dt = get_sim_now()
        if clock_dt is None:
            # no sim clock -> just set stopped
            running_after = False

        if running_after:
            cur.execute("""
                UPDATE timers
                SET elapsed=?, running=1, start_sim_iso=?, start_epoch=NULL
                WHERE mode=? AND timer_id=?
            """, (new_total_sec, clock_dt.replace(tzinfo=None).isoformat(timespec="seconds"), mode, timer_id))
        else:
            cur.execute("""
                UPDATE timers
                SET elapsed=?, running=0, start_sim_iso=NULL, start_epoch=NULL
                WHERE mode=? AND timer_id=?
            """, (new_total_sec, mode, timer_id))

    conn.commit()
    conn.close()

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
    Your rules:
    - 00:00-07:59 => write to 23 of previous day (row 23-24)
    - hour >= 24  => clamp to 23 (row 23-24)
    - else use hour
    """
    if clock_dt.hour < FIRST_LOG_HOUR:
        return 23, (clock_dt.date() - timedelta(days=1))
    if clock_dt.hour >= 24:
        return 23, clock_dt.date()
    if clock_dt.hour > 23:
        return 23, clock_dt.date()
    return clock_dt.hour, clock_dt.date()

def log_to_sheet(mode: str, clock_dt: datetime, force=False):
    ws = gs_connect()
    if ws is None:
        return False, "Google creds missing"

    if clock_dt is None:
        return False, "Clock missing"

    hour, day = target_hour_and_date(clock_dt)

    last_h = get_meta(f"last_logged_hour_{mode}")
    last_d = get_meta(f"last_logged_day_{mode}")
    day_key = day.isoformat()

    if not force and last_h == str(hour) and last_d == day_key:
        return True, "already logged"

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return False, f"Date {date_str} not found in sheet row 3"

    col = headers.index(date_str) + 1

    # rows: row 7 = 08:00, row 22 = 23:00
    row = 7 + (hour - 8)
    if row < 7:
        row = 7
    if row > 22:
        row = 22

    # total = sum timers of THIS mode
    total = 0
    for i in range(1, TIMER_COUNT + 1):
        total += timer_total_seconds(mode, i, clock_dt)

    ws.update_cell(row, col, fmt(total))

    set_meta(f"last_logged_hour_{mode}", str(hour))
    set_meta(f"last_logged_day_{mode}", day_key)
    return True, "logged"

def maybe_auto_log_for_mode(mode: str, clock_dt: datetime):
    """
    Auto log on exact HH:00:00 when hour in 08..24.
    Note: 24 is mapped to 23 row anyway, per rules.
    """
    if clock_dt is None:
        return
    if clock_dt.minute == 0 and clock_dt.second == 0:
        if FIRST_LOG_HOUR <= clock_dt.hour <= LAST_LOG_HOUR:
            log_to_sheet(mode, clock_dt, force=False)

# =========================
# START TIME LOG (Row 4) - per mode
# =========================
def log_start_time_if_needed(mode: str, clock_dt: datetime):
    """
    Writes HH:MM to row 4 for the DATE of clock_dt,
    first Start after 05:00, per mode.
    """
    if clock_dt is None:
        return
    if clock_dt.hour < RESET_HOUR:
        return

    ws = gs_connect()
    if ws is None:
        return

    # Align with sheet date rule
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
    n = now()
    mode = current_mode()

    # If sim enabled but missing clock -> disable
    if sim_enabled() and n is None:
        set_meta("sim_enabled", "0")
        set_meta("sim_now_iso", "")
        n = tz_now_real()
        mode = "real"

    # Advance sim clock by 1 second per status tick
    if sim_enabled():
        set_sim_now(n + timedelta(seconds=1))
        n = get_sim_now()

    # Reset per mode (based on each clock)
    ensure_daily_reset_for_mode("real", tz_now_real())
    ensure_daily_reset_for_mode("sim", get_sim_now() if sim_enabled() else None)

    # Auto log per mode (both)
    maybe_auto_log_for_mode("real", tz_now_real())
    if sim_enabled():
        maybe_auto_log_for_mode("sim", n)

    timers = [fmt(timer_total_seconds(mode, i, n)) for i in range(1, TIMER_COUNT + 1)]
    running = []
    for i in range(1, TIMER_COUNT + 1):
        t = timer_row(mode, i)
        running.append(bool(int(t["running"])) if t else False)

    return jsonify({
        "now_str": n.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled(),
        "mode": mode,
        "timers": timers,
        "running": running
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock_dt = now() if mode == "sim" else tz_now_real()
    if clock_dt is None:
        clock_dt = tz_now_real()

    ensure_daily_reset_for_mode(mode, clock_dt)

    # write start time (row 4) on first start after 05:00 for this mode
    log_start_time_if_needed(mode, clock_dt)

    t = timer_row(mode, i)
    if t is None:
        return jsonify(error="timer missing"), 500

    if int(t["running"]) == 0:
        conn = db()
        cur = conn.cursor()
        if mode == "real":
            cur.execute("""
                UPDATE timers
                SET running=1, start_epoch=?, start_sim_iso=NULL
                WHERE mode=? AND timer_id=?
            """, (tz_now_real().timestamp(), mode, i))
        else:
            cur.execute("""
                UPDATE timers
                SET running=1, start_sim_iso=?, start_epoch=NULL
                WHERE mode=? AND timer_id=?
            """, (clock_dt.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))
        conn.commit()
        conn.close()

    return jsonify(ok=True)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    clock_dt = now() if mode == "sim" else tz_now_real()
    if clock_dt is None:
        clock_dt = tz_now_real()

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

# ---------- EDIT: adjust (delta seconds) ----------
@app.route("/api/timer/<int:i>/adjust", methods=["POST"])
def adjust_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    data = request.get_json(force=True)
    delta = int(data.get("delta", 0))

    mode = current_mode()
    clock_dt = now() if mode == "sim" else tz_now_real()
    if clock_dt is None:
        clock_dt = tz_now_real()

    t = timer_row(mode, i)
    if t is None:
        return jsonify(error="timer missing"), 500

    was_running = int(t["running"]) == 1
    if was_running and not ALLOW_EDIT_WHILE_RUNNING:
        return jsonify(error="cannot edit while running"), 400

    current = timer_total_seconds(mode, i, clock_dt)
    new_val = max(0, current + delta)

    # keep running if it was running (and allowed)
    set_timer_total(mode, i, new_val, keep_running=True, clock_dt=clock_dt)
    return jsonify(ok=True, new_time=fmt(new_val))

# ---------- EDIT: set absolute time (HH:MM:SS) ----------
@app.route("/api/timer/<int:i>/set", methods=["POST"])
def set_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    data = request.get_json(force=True)
    value = str(data.get("value", "")).strip()

    try:
        new_val = hms_to_seconds(value)
    except Exception:
        return jsonify(error="bad format, use HH:MM:SS or MM:SS"), 400

    mode = current_mode()
    clock_dt = now() if mode == "sim" else tz_now_real()
    if clock_dt is None:
        clock_dt = tz_now_real()

    t = timer_row(mode, i)
    if t is None:
        return jsonify(error="timer missing"), 500

    was_running = int(t["running"]) == 1
    if was_running and not ALLOW_EDIT_WHILE_RUNNING:
        return jsonify(error="cannot edit while running"), 400

    set_timer_total(mode, i, new_val, keep_running=True, clock_dt=clock_dt)
    return jsonify(ok=True, new_time=fmt(new_val))

# ---------- GOOGLE: manual log ----------
@app.route("/api/log-now", methods=["POST"])
def manual_log():
    mode = current_mode()
    clock_dt = now() if mode == "sim" else tz_now_real()
    if clock_dt is None:
        clock_dt = tz_now_real()

    ok, msg = log_to_sheet(mode, clock_dt, force=True)
    return jsonify(ok=ok, message=msg), (200 if ok else 500)

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
    app.run(host="127.0.0.1", port=5000, debug=True)