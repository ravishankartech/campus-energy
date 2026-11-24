
import csv, io, math, os, random, sqlite3, threading, time
from datetime import datetime, timedelta
from flask import Flask, jsonify, request, Response, send_from_directory

DB = "campus.db"
TARIFF = 8.0          # Rs per kWh
SIM_INTERVAL = 5      # seconds between simulated readings
app = Flask(__name__, static_folder="static")

BUILDINGS = [  # name, type, base kW, peak kW, alert limit kW
    ("Academic Block A", "Academic", 25, 85, 95),
    ("Central Library", "Academic", 15, 45, 55),
    ("Boys Hostel", "Hostel", 30, 90, 100),
    ("Girls Hostel", "Hostel", 28, 80, 90),
    ("Admin Building", "Office", 10, 35, 42),
    ("Cafeteria", "Food", 12, 50, 58),
]


def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS buildings(id INTEGER PRIMARY KEY, name TEXT, type TEXT,
        base REAL, peak REAL, limit_kw REAL);
    CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, building_id INT, ts TEXT, kw REAL);
    CREATE INDEX IF NOT EXISTS ix_r ON readings(building_id, ts);
    CREATE TABLE IF NOT EXISTS faults(id INTEGER PRIMARY KEY, building_id INT, category TEXT,
        description TEXT, priority TEXT, status TEXT DEFAULT 'Open', reporter TEXT,
        source TEXT DEFAULT 'Manual', created TEXT, updated TEXT);
    """)
    if not c.execute("SELECT 1 FROM buildings").fetchone():
        for i, b in enumerate(BUILDINGS, 1):
            c.execute("INSERT INTO buildings VALUES(?,?,?,?,?,?)", (i, *b))
        now = datetime.now()
        for h in range(24 * 3 * 4, 0, -1):  # 3 days of history, every 15 min
            t = now - timedelta(minutes=15 * h)
            for i, b in enumerate(BUILDINGS, 1):
                c.execute("INSERT INTO readings(building_id,ts,kw) VALUES(?,?,?)",
                          (i, t.isoformat(timespec="seconds"), sim_kw(b[2], b[3], t, spike=False)))
        for bid, cat, d, p, s in [
            (3, "Electrical", "Corridor lights flickering on 2nd floor", "Medium", "Open"),
            (1, "HVAC", "AC in Lab 204 not cooling", "High", "In Progress"),
            (2, "Water/Plumbing", "Tap leaking near reading hall", "Low", "Resolved")]:
            ts = (now - timedelta(hours=random.randint(3, 40))).isoformat(timespec="seconds")
            c.execute("INSERT INTO faults(building_id,category,description,priority,status,reporter,created,updated)"
                      " VALUES(?,?,?,?,?,?,?,?)", (bid, cat, d, p, s, "Demo user", ts, ts))
        c.commit()
    c.close()


def sim_kw(base, peak, t, spike=True):
    """Daily usage curve: low at night, peak ~2 pm, with noise and rare spikes."""
    h = t.hour + t.minute / 60
    load = base + (peak - base) * max(0, math.sin((h - 6) / 16 * math.pi)) if 6 <= h <= 22 else base
    kw = load * random.uniform(0.92, 1.08)
    if spike and random.random() < 0.01:
        kw *= random.uniform(1.4, 1.8)   # simulated fault / wastage
    return round(kw, 2)


def check_alert(c, bid, kw):
    """Auto-raise a fault when consumption crosses the building limit."""
    b = c.execute("SELECT * FROM buildings WHERE id=?", (bid,)).fetchone()
    if kw <= b["limit_kw"]:
        return
    if c.execute("SELECT 1 FROM faults WHERE building_id=? AND source='Auto' AND status!='Resolved'", (bid,)).fetchone():
        return
    now = datetime.now().isoformat(timespec="seconds")
    c.execute("INSERT INTO faults(building_id,category,description,priority,reporter,source,created,updated)"
              " VALUES(?,?,?,?,?,?,?,?)",
              (bid, "Energy Wastage", f"Auto-detected: {kw} kW exceeds limit of {b['limit_kw']} kW",
               "High", "System", "Auto", now, now))


def simulator():
    while True:
        time.sleep(SIM_INTERVAL)
        c = db()
        now = datetime.now()
        for b in c.execute("SELECT * FROM buildings").fetchall():
            kw = sim_kw(b["base"], b["peak"], now)
            c.execute("INSERT INTO readings(building_id,ts,kw) VALUES(?,?,?)",
                      (b["id"], now.isoformat(timespec="seconds"), kw))
            check_alert(c, b["id"], kw)
        c.commit()
        c.close()


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/buildings")
def buildings():
    c = db()
    return jsonify([dict(r) for r in c.execute("SELECT id,name,type,limit_kw FROM buildings")])


@app.get("/api/summary")
def summary():
    c = db()
    today = datetime.now().strftime("%Y-%m-%d")
    out, total_now, total_kwh = [], 0, 0
    for b in c.execute("SELECT * FROM buildings").fetchall():
        last = c.execute("SELECT kw FROM readings WHERE building_id=? ORDER BY id DESC LIMIT 1", (b["id"],)).fetchone()
        rows = c.execute("SELECT ts,kw FROM readings WHERE building_id=? AND ts>=? ORDER BY ts", (b["id"], today)).fetchall()
        kwh = sum(r["kw"] * 0.25 for r in rows)   # readings treated as 15-min samples
        kw = last["kw"] if last else 0
        total_now += kw
        total_kwh += kwh
        out.append({"id": b["id"], "name": b["name"], "kw": kw, "limit": b["limit_kw"],
                    "kwh_today": round(kwh, 1), "alert": kw > b["limit_kw"]})
    open_faults = c.execute("SELECT COUNT(*) n FROM faults WHERE status!='Resolved'").fetchone()["n"]
    return jsonify({"buildings": out, "total_kw": round(total_now, 1), "kwh_today": round(total_kwh, 1),
                    "cost_today": round(total_kwh * TARIFF), "tariff": TARIFF, "open_faults": open_faults})


@app.get("/api/readings")
def readings_get():
    bid = request.args.get("building_id", type=int)
    hours = request.args.get("hours", 24, type=int)
    since = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    c = db()
    if bid:
        rows = c.execute("SELECT ts,kw FROM readings WHERE building_id=? AND ts>=? ORDER BY ts", (bid, since)).fetchall()
    else:  # campus total, grouped by minute bucket
        rows = c.execute("SELECT substr(ts,1,15) || '0:00' ts, SUM(kw) kw FROM readings WHERE ts>=? "
                         "GROUP BY substr(ts,1,15) ORDER BY ts", (since,)).fetchall()
    return jsonify([{"ts": r["ts"], "kw": round(r["kw"], 2)} for r in rows][-400:])


@app.post("/api/readings")
def readings_post():
    """Endpoint for real IoT nodes (ESP32 / STM32 / smart meters)."""
    d = request.get_json(force=True, silent=True) or {}
    try:
        bid, kw = int(d["building_id"]), float(d["kw"])
    except (KeyError, ValueError, TypeError):
        return jsonify(error="building_id and kw are required"), 400
    c = db()
    c.execute("INSERT INTO readings(building_id,ts,kw) VALUES(?,?,?)",
              (bid, datetime.now().isoformat(timespec="seconds"), kw))
    check_alert(c, bid, kw)
    c.commit()
    return jsonify(ok=True), 201


@app.get("/api/faults")
def faults_get():
    status = request.args.get("status")
    q = ("SELECT f.*, b.name building FROM faults f JOIN buildings b ON b.id=f.building_id"
         + (" WHERE f.status=?" if status else "") + " ORDER BY f.id DESC")
    c = db()
    return jsonify([dict(r) for r in c.execute(q, (status,) if status else ())])


@app.post("/api/faults")
def faults_post():
    d = request.get_json(force=True, silent=True) or {}
    desc = (d.get("description") or "").strip()
    if len(desc) < 5 or not d.get("building_id"):
        return jsonify(error="Building and a description (min 5 characters) are required"), 400
    pr = d.get("priority") if d.get("priority") in ("Low", "Medium", "High") else "Medium"
    now = datetime.now().isoformat(timespec="seconds")
    c = db()
    cur = c.execute("INSERT INTO faults(building_id,category,description,priority,reporter,created,updated)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (int(d["building_id"]), d.get("category", "Other"), desc[:500], pr,
                     (d.get("reporter") or "Anonymous")[:60], now, now))
    c.commit()
    return jsonify(id=cur.lastrowid), 201


@app.patch("/api/faults/<int:fid>")
def faults_patch(fid):
    st = (request.get_json(force=True, silent=True) or {}).get("status")
    if st not in ("Open", "In Progress", "Resolved"):
        return jsonify(error="invalid status"), 400
    c = db()
    c.execute("UPDATE faults SET status=?, updated=? WHERE id=?",
              (st, datetime.now().isoformat(timespec="seconds"), fid))
    c.commit()
    return jsonify(ok=True)


def csv_response(rows, name):
    buf = io.StringIO()
    w = csv.writer(buf)
    if rows:
        w.writerow(rows[0].keys())
        w.writerows([tuple(r) for r in rows])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}.csv"})


@app.get("/api/export/<what>.csv")
def export(what):
    c = db()
    if what == "faults":
        rows = c.execute("SELECT f.id,b.name building,category,description,priority,status,reporter,source,created,updated "
                         "FROM faults f JOIN buildings b ON b.id=f.building_id ORDER BY f.id DESC").fetchall()
    elif what == "readings":
        rows = c.execute("SELECT r.ts,b.name building,r.kw FROM readings r JOIN buildings b ON b.id=r.building_id "
                         "ORDER BY r.ts DESC LIMIT 20000").fetchall()
    else:
        return jsonify(error="unknown export"), 404
    return csv_response(rows, what)


init_db()
if os.environ.get("RUN_SIMULATOR", "1") == "1":
    threading.Thread(target=simulator, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))