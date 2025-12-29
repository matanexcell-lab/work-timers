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
        elapsed INTEGER NOT NULL DEFAULT 0,  -- seconds accumulated (when not running)
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
        "last_reset_date": "",
        "last_logged_hour": "",
        "last_logged_day": "",   # date (YYYY-MM-DD) associated with last_logged_hour
    }
    for k, v in defaults.items():
        cur.execute("INSERT OR IGNORE INTO meta(k, v) VALUES(?, ?)", (k, v))

    conn.commit()
    conn.close()

init_db()

# =========================
# TIME HELPERS
# =========================
def tz_now_real():
    return datetime.now(TZ)

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

def sim_enabled() -> bool:
    return get_meta("sim_enabled") == "1"

def get_sim_now() -> datetime | None:
    iso = get_meta("sim_now_iso")
    if not iso:
        return None
    # stored as ISO without timezone offset; treat as TZ-localized
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = TZ.localize(dt)
    return dt.astimezone(TZ)

def set_sim_now(dt: datetime):
    dt = dt.astimezone(TZ)
    set_meta("sim_now_iso", dt.replace(tzinfo=None).isoformat(timespec="seconds"))

def now():
    if sim_enabled():
        return get_sim_now()
    return tz_now_real()

def current_mode():
    return "sim" if sim_enabled() else "real"

# =========================
# RESET LOGIC (05:00)
# =========================
def ensure_daily_reset(n: datetime):
    """
    If time is >= 05:00 and we haven't reset today -> reset REAL timers.
    """
    if n is None:
        return

    if n.hour < RESET_HOUR:
        return

    last = get_meta("last_reset_date")  # YYYY-MM-DD
    today = n.date().isoformat()
    if last == today:
        return

    # reset real timers only
    conn = db()
    cur = conn.cursor()
    for i in range(1, TIMER_COUNT + 1):
        cur.execute("""
        UPDATE timers
        SET running=0, elapsed=0, start_epoch=NULL, start_sim_iso=NULL
        WHERE mode='real' AND timer_id=?
        """, (i,))
    conn.commit()
    conn.close()

    set_meta("last_reset_date", today)

# =========================
# TIMER CALCULATION (no background thread)
# =========================
def timer_total_seconds(mode: str, timer_id: int, n: datetime) -> int:
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
        # real uses epoch seconds
        if t["start_epoch"] is None:
            return elapsed
        # use current real time (not sim)
        now_epoch = tz_now_real().timestamp()
        return elapsed + int(max(0, now_epoch - float(t["start_epoch"])))

    # sim mode: use sim_now as clock
    if t["start_sim_iso"] is None:
        return elapsed
    start_dt = datetime.fromisoformat(t["start_sim_iso"])
    if start_dt.tzinfo is None:
        start_dt = TZ.localize(start_dt)
    diff = int(max(0, (n - start_dt).total_seconds()))
    return elapsed + diff

def fmt(sec: int) -> str:
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

def target_hour_and_date(n: datetime):
    """
    Your rule:
    - If time is after midnight but before 08:00 -> write to 23 of previous day
    - Hours are only 08..23 and special 24 (but sheet typically ends at 23 line).
      You asked: after 12AM, write between 23-24 => we will use 23 row.
    """
    if n.hour < FIRST_LOG_HOUR:
        return 23, (n.date() - timedelta(days=1))
    if n.hour > 23:
        return 23, n.date()
    return n.hour, n.date()

def log_to_sheet(force=False):
    ws = gs_connect()
    if ws is None:
        return False, "Google creds missing"

    n = tz_now_real()  # logging is ALWAYS for real mode
    hour, day = target_hour_and_date(n)

    last_h = get_meta("last_logged_hour")
    last_d = get_meta("last_logged_day")

    day_str = day.isoformat()
    if not force and last_h == str(hour) and last_d == day_str:
        return True, "already logged"

    date_str = day.strftime("%d/%m/%Y")
    headers = ws.row_values(3)
    if date_str not in headers:
        return False, f"Date {date_str} not found in sheet row 3"

    col = headers.index(date_str) + 1

    # rows: 8..23 are 7..22 offsets (row 7 is 08:00)
    # hour==23 -> row 7+(23-8)=22
    row = 7 + (hour - 8)
    if row < 7:
        row = 7
    if row > 22:
        row = 22

    # total = sum REAL timers (computed from DB + running)
    total = 0
    for i in range(1, TIMER_COUNT + 1):
        total += timer_total_seconds("real", i, tz_now_real())

    ws.update_cell(row, col, fmt(total))

    set_meta("last_logged_hour", str(hour))
    set_meta("last_logged_day", day_str)
    return True, "logged"

def maybe_auto_log(n: datetime):
    """
    Called on /api/status every second from client.
    We log if real time is exactly at HH:00:00 and in 08..24.
    After midnight: we will still log as per rule (to 23 prev day) ONLY if called at 00:00:00 etc,
    but your main need is manual/automatic during work hours.
    """
    r = tz_now_real()
    if r.minute == 0 and r.second == 0:
        if FIRST_LOG_HOUR <= r.hour <= LAST_LOG_HOUR:
            log_to_sheet(force=False)

# =========================
# ROUTES
# =========================
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    n = now()
    if n is None:
        # if sim enabled but sim_now missing -> fix
        set_meta("sim_enabled", "0")
        n = tz_now_real()

    # advance sim clock by 1 sec (because client calls status every second)
    if sim_enabled():
        set_sim_now(n + timedelta(seconds=1))
        n = get_sim_now()

    ensure_daily_reset(tz_now_real())
    maybe_auto_log(n)

    mode = current_mode()

    timers = []
    for i in range(1, TIMER_COUNT + 1):
        timers.append(fmt(timer_total_seconds(mode, i, n)))

    return jsonify({
        "now_str": n.strftime("%d/%m/%Y %H:%M:%S"),
        "simulation": sim_enabled(),
        "timers": timers
    })

@app.route("/api/timer/<int:i>/start", methods=["POST"])
def start_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    n = now()
    if n is None:
        n = tz_now_real()

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
            """, (n.replace(tzinfo=None).isoformat(timespec="seconds"), mode, i))

    conn.commit()
    conn.close()
    return ("", 204)

@app.route("/api/timer/<int:i>/stop", methods=["POST"])
def stop_timer(i):
    if i < 1 or i > TIMER_COUNT:
        return jsonify(error="bad timer id"), 400

    mode = current_mode()
    n = now()
    if n is None:
        n = tz_now_real()

    # compute current total, then store it in elapsed and clear start
    total = timer_total_seconds(mode, i, n)

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

@app.route("/api/log-now", methods=["POST"])
def manual_log():
    ok, msg = log_to_sheet(force=True)
    return jsonify(ok=ok, message=msg), (200 if ok else 500)

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