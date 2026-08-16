#!/usr/bin/env python3
"""Marrow self-hosted server — pair once, own your mirror, serve your agents.

One file, Python stdlib only. Run it on anything that stays on (Raspberry
Pi, NAS, home server, old laptop):

    python3 marrow_server.py                # prints the pairing QR page URL
    python3 marrow_server.py --port 8800 --data ./data

Then in Marrow on your phone: Server → Pair with a server → scan.

What it does:
  - receives Marrow's background pushes (phone app can stay closed),
  - keeps a complete SQLite mirror of your verified health data,
  - serves it to AI agents over MCP (Claude, Cursor, any MCP client),
    24/7 — this is the always-on companion to the app's on-device
    Live mode.

Your data flows only from your phone to this machine. No cloud, no vendor,
no telemetry. Put it behind HTTPS (tailscale serve, Caddy) if you want to
reach it from outside your LAN — never port-forward it raw.

Endpoints (app token via X-HealthBridge-Token; agents via Authorization:
Bearer <mcp token> — two separate tokens, both auto-generated in --data):
    GET  /pair     pairing page (QR) — open on a second screen, scan
    POST /ping     pairing test
    POST /ingest   sample batches from the app (idempotent, uuid upsert)
    GET  /status   mirror summary
    POST /mcp      MCP Streamable HTTP (JSON-RPC 2.0)
    GET  /health   liveness
"""

import argparse
import hmac
import json
import re
import secrets
import socket
import sqlite3
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

PROTOCOL_VERSION = "2025-06-18"
MAX_BODY = 32 * 1024 * 1024

# App stream key -> Apple-native type. `scale` converts wire value back to
# Apple-native storage (the one that matters: SpO2 travels as 97, stores as
# the 0.97 fraction Apple uses).
KEYMAP = {
    "step_count":               ("HKQuantityTypeIdentifierStepCount", 1),
    "walking_running_distance": ("HKQuantityTypeIdentifierDistanceWalkingRunning", 1),
    "flights_climbed":          ("HKQuantityTypeIdentifierFlightsClimbed", 1),
    "active_energy":            ("HKQuantityTypeIdentifierActiveEnergyBurned", 1),
    "basal_energy_burned":      ("HKQuantityTypeIdentifierBasalEnergyBurned", 1),
    "apple_exercise_time":      ("HKQuantityTypeIdentifierAppleExerciseTime", 1),
    "apple_stand_time":         ("HKQuantityTypeIdentifierAppleStandTime", 1),
    "heart_rate":               ("HKQuantityTypeIdentifierHeartRate", 1),
    "resting_heart_rate":       ("HKQuantityTypeIdentifierRestingHeartRate", 1),
    "heart_rate_variability":   ("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", 1),
    "respiratory_rate":         ("HKQuantityTypeIdentifierRespiratoryRate", 1),
    "blood_oxygen":             ("HKQuantityTypeIdentifierOxygenSaturation", 0.01),
    "body_mass":                ("HKQuantityTypeIdentifierBodyMass", 1),
}

STAGEMAP = {
    "core":    "HKCategoryValueSleepAnalysisAsleepCore",
    "deep":    "HKCategoryValueSleepAnalysisAsleepDeep",
    "rem":     "HKCategoryValueSleepAnalysisAsleepREM",
    "asleep":  "HKCategoryValueSleepAnalysisAsleepUnspecified",
    "awake":   "HKCategoryValueSleepAnalysisAwake",
    "in_bed":  "HKCategoryValueSleepAnalysisInBed",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    uuid           TEXT UNIQUE,
    type           TEXT NOT NULL,
    source_name    TEXT,
    source_version TEXT,
    device         TEXT,
    unit           TEXT,
    creation_date  TEXT,
    start_date     TEXT,
    end_date       TEXT,
    value          TEXT,
    num            REAL,
    day            TEXT
);
CREATE INDEX IF NOT EXISTS idx_records_type_day ON records (type, day);
CREATE INDEX IF NOT EXISTS idx_records_day ON records (day);
CREATE TABLE IF NOT EXISTS workouts (
    id             INTEGER PRIMARY KEY,
    uuid           TEXT UNIQUE,
    activity_type  TEXT,
    duration       REAL,
    duration_unit  TEXT,
    source_name    TEXT,
    device         TEXT,
    creation_date  TEXT,
    start_date     TEXT,
    end_date       TEXT,
    day            TEXT
);
CREATE TABLE IF NOT EXISTS ingest_log (
    ts TEXT, seq INTEGER, stream TEXT, added INTEGER, deleted INTEGER
);
"""

SUM_HINTS = ("StepCount", "Distance", "FlightsClimbed", "EnergyBurned",
             "ExerciseTime", "StandTime", "MoveTime", "Dietary", "PushCount",
             "SwimmingStrokeCount", "TimeInDaylight", "NumberOf",
             "InhalerUsage", "InsulinDelivery")


def short_key(apple_type):
    t = re.sub(r"^HK(Quantity|Category)TypeIdentifier", "", apple_type)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", t).lower()


def load_token(path):
    if path.exists():
        return path.read_text().strip()
    tok = secrets.token_urlsafe(24)
    path.write_text(tok)
    path.chmod(0o600)
    return tok


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def local_strings(iso_utc, offset_s):
    """UTC instant -> device-local strings. The day must come from the
    DEVICE's timezone — server-side days are the classic off-by-one."""
    dt = datetime.fromisoformat(iso_utc.replace("Z", "+00:00"))
    dt = dt.astimezone(timezone(timedelta(seconds=offset_s)))
    return dt.strftime("%Y-%m-%d %H:%M:%S %z"), dt.strftime("%Y-%m-%d")


class Mirror:
    def __init__(self, data_dir):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = data_dir / "mirror.sqlite"
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.executescript(SCHEMA)
        self.staging = data_dir / "staging"
        self.staging.mkdir(exist_ok=True)

    # ---- ingest (Marrow push protocol; idempotent on uuid) ----

    def ingest(self, payload):
        stream = str(payload.get("stream") or "")
        hk_type = str(payload.get("hk_type") or "")
        try:
            native_scale = float(payload.get("native_scale") or 1)
        except (TypeError, ValueError):
            native_scale = 1.0
        try:
            offset = int(payload.get("tz_offset_seconds") or 0)
        except (TypeError, ValueError):
            offset = 0
        added = payload.get("added") or []
        deleted = payload.get("deleted") or []
        if not isinstance(added, list) or not isinstance(deleted, list) \
                or not all(isinstance(x, dict) for x in added):
            raise ValueError("added/deleted must be lists (of objects)")

        # Raw staging first — everything downstream is rebuildable.
        stamp = datetime.now().strftime("%Y-%m-%d")
        with open(self.staging / f"{stamp}.jsonl", "a") as fh:
            fh.write(json.dumps(payload, separators=(",", ":")) + "\n")

        n_add = n_del = n_ignored = 0
        cur = self.conn.cursor()
        for s in added:
            uuid = str(s.get("uuid") or "")
            start = str(s.get("start") or "")
            end = str(s.get("end") or "")
            if not uuid or not start or not end:
                n_ignored += 1
                continue
            try:
                start_s, day = local_strings(start, offset)
                end_s, _ = local_strings(end, offset)
            except ValueError:
                n_ignored += 1
                continue
            source = str(s.get("source") or "")
            device = str(s.get("device") or "")
            unit = str(s.get("unit") or "")
            value = s.get("value")

            if stream == "workout":
                cur.execute("""
                    INSERT INTO workouts(uuid,activity_type,duration,duration_unit,
                        source_name,device,start_date,end_date,day)
                    VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(uuid) DO UPDATE SET duration=excluded.duration,
                        start_date=excluded.start_date, end_date=excluded.end_date,
                        day=excluded.day
                    """, (uuid,
                          "HKWorkoutActivityType" + str(s.get("stage") or "Workout"),
                          value, unit, source, device, start_s, end_s, day))
                n_add += 1
                continue

            if stream == "sleep":
                row_type = "HKCategoryTypeIdentifierSleepAnalysis"
                text_value = STAGEMAP.get(str(s.get("stage") or ""),
                                          STAGEMAP["asleep"])
                num = None
            elif stream in KEYMAP:
                row_type, scale = KEYMAP[stream]
                try:
                    num = float(value) * scale
                except (TypeError, ValueError):
                    n_ignored += 1
                    continue
                text_value = repr(num)
            elif hk_type.startswith("HK"):
                # Generic path: payload carries its own Apple-native type +
                # scale, so app streams added later land with zero server
                # updates.
                row_type = hk_type
                try:
                    num = float(value) * native_scale
                except (TypeError, ValueError):
                    n_ignored += 1
                    continue
                text_value = repr(num)
            else:
                n_ignored += 1   # unknown, kept in staging; never 4xx
                continue

            cur.execute("""
                INSERT INTO records(uuid,type,source_name,device,unit,
                    start_date,end_date,value,num,day)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(uuid) DO UPDATE SET num=excluded.num,
                    value=excluded.value, start_date=excluded.start_date,
                    end_date=excluded.end_date, day=excluded.day
                """, (uuid, row_type, source, device, unit,
                      start_s, end_s, text_value, num, day))
            n_add += 1

        for u in deleted:
            cur.execute("DELETE FROM records WHERE uuid=?", (str(u),))
            cur.execute("DELETE FROM workouts WHERE uuid=?", (str(u),))
            n_del += cur.rowcount

        cur.execute("INSERT INTO ingest_log VALUES(?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"),
                     payload.get("seq"), stream, n_add, n_del))
        self.conn.commit()
        return {"status": "ok", "added": n_add, "deleted": n_del,
                "ignored": n_ignored}

    def status(self):
        c = self.conn
        total, days, lo, hi = c.execute(
            "SELECT COUNT(*), COUNT(DISTINCT day), MIN(day), MAX(day) "
            "FROM records").fetchone()
        last = c.execute("SELECT MAX(ts) FROM ingest_log").fetchone()[0]
        return {"records": total, "days": days, "first_day": lo,
                "last_day": hi, "last_ingest": last}

    # ---- reads for MCP ----

    def types(self):
        return self.conn.execute(
            "SELECT type, unit, COUNT(*), MIN(day), MAX(day) FROM records "
            "GROUP BY type ORDER BY 3 DESC").fetchall()

    def resolve(self, key):
        for (t,) in self.conn.execute("SELECT DISTINCT type FROM records"):
            if t == key or short_key(t) == key:
                return t
        return None

    def daily(self, apple_type, days):
        agg = "SUM" if any(h in apple_type for h in SUM_HINTS) else "AVG"
        floor = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return self.conn.execute(
            f"SELECT day, {agg}(num) FROM records "
            "WHERE type=? AND day>=? AND num IS NOT NULL "
            "GROUP BY day ORDER BY day", (apple_type, floor)).fetchall()

    def samples(self, apple_type, days, limit):
        floor = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return self.conn.execute(
            "SELECT start_date, end_date, value, num, source_name FROM records "
            "WHERE type=? AND day>=? ORDER BY start_date DESC LIMIT ?",
            (apple_type, floor, limit)).fetchall()

    def workouts(self, days):
        floor = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        return self.conn.execute(
            "SELECT activity_type, start_date, end_date, duration, "
            "duration_unit, source_name FROM workouts WHERE day>=? "
            "ORDER BY start_date DESC", (floor,)).fetchall()


TOOLS = [
    {"name": "health_summary",
     "description": "Daily values for core metrics over recent days.",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer",
                  "description": "Recent days (default 3, max 30)"}}}},
    {"name": "list_metrics",
     "description": "Every metric in the mirror with units and coverage; "
                    "use the 'key' values in the other tools.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "metric_daily",
     "description": "Daily aggregated values for one metric.",
     "inputSchema": {"type": "object", "properties": {
         "metric": {"type": "string", "description": "Metric key"},
         "days": {"type": "integer",
                  "description": "Days back (default 30, max 400)"}},
         "required": ["metric"]}},
    {"name": "metric_samples",
     "description": "Raw records for one metric.",
     "inputSchema": {"type": "object", "properties": {
         "metric": {"type": "string", "description": "Metric key"},
         "days": {"type": "integer", "description": "Days back (default 7)"},
         "limit": {"type": "integer",
                   "description": "Max records (default 200, max 1000)"}},
         "required": ["metric"]}},
    {"name": "workouts",
     "description": "Workouts in the mirror (all sources).",
     "inputSchema": {"type": "object", "properties": {
         "days": {"type": "integer", "description": "Days back (default 30)"}}}},
]

SUMMARY_KEYS = ["step_count", "active_energy_burned", "apple_exercise_time",
                "resting_heart_rate", "heart_rate_variability_s_d_n_n",
                "body_mass", "dietary_energy_consumed"]


def call_tool(mirror, name, args):
    def clamp(key, default, cap):
        try:
            v = int(args.get(key, default))
        except (TypeError, ValueError):
            v = default
        return max(1, min(v, cap))

    if name == "list_metrics":
        return [{"key": short_key(t), "unit": u or "", "records": n,
                 "first_day": lo, "latest_day": hi}
                for t, u, n, lo, hi in mirror.types()]
    if name == "health_summary":
        days = clamp("days", 3, 30)
        out = {}
        for key in SUMMARY_KEYS:
            t = mirror.resolve(key)
            if t:
                series = mirror.daily(t, days)
                if series:
                    out[key] = {d: round(v, 2) for d, v in series}
        return out
    if name in ("metric_daily", "metric_samples"):
        t = mirror.resolve(args.get("metric", ""))
        if not t:
            raise ValueError(f"unknown metric: {args.get('metric')} "
                             "(see list_metrics)")
        if name == "metric_daily":
            return {"metric": short_key(t),
                    "days": {d: round(v, 3)
                             for d, v in mirror.daily(t, clamp("days", 30, 400))}}
        rows = mirror.samples(t, clamp("days", 7, 400), clamp("limit", 200, 1000))
        return [{"start": s, "end": e,
                 "value": num if num is not None else v, "source": src}
                for s, e, v, num, src in rows]
    if name == "workouts":
        return [{"activity": a, "start": s, "end": e, "duration": d,
                 "unit": u, "source": src}
                for a, s, e, d, u, src in mirror.workouts(clamp("days", 30, 400))]
    raise ValueError(f"unknown tool: {name}")


def pair_page(pairing):
    info = json.dumps(pairing)
    try:
        import segno
        import base64
        import io
        buf = io.BytesIO()
        segno.make(info).save(buf, kind="png", scale=8)
        img = base64.b64encode(buf.getvalue()).decode()
        qr = f'<img src="data:image/png;base64,{img}" alt="pairing QR">'
    except ImportError:
        qr = "<p>(install <code>segno</code> for a QR; manual JSON below)</p>"
    return f"""<!doctype html><meta charset="utf-8">
<title>Pair Marrow</title>
<body style="font-family:-apple-system,sans-serif;max-width:32em;margin:3em auto">
<h2>Pair Marrow with this server</h2>
<p>On your iPhone: <b>Marrow → Server → Pair with a server</b>, then scan:</p>
{qr}
<p style="color:#666">Manual fallback — pairing JSON:</p>
<pre style="background:#f4f4f2;padding:1em;overflow:auto">{info}</pre>
</body>""".encode()


def make_handler(mirror, app_token, mcp_token, pairing):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Marrow/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code, obj=None):
            body = json.dumps(obj).encode() if obj is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _app_authed(self):
            q = parse_qs(urlparse(self.path).query)
            supplied = (self.headers.get("X-HealthBridge-Token")
                        or (q.get("token") or [""])[0])
            return hmac.compare_digest(supplied, app_token)

        def _mcp_authed(self):
            return hmac.compare_digest(self.headers.get("Authorization", ""),
                                       f"Bearer {mcp_token}")

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/")
            if path in ("", "/health"):
                return self._json(200, {"server": "marrow", "ok": True})
            if not self._app_authed():
                return self._json(401, {"error": "bad or missing token"})
            if path == "/pair":
                body = pair_page(pairing)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if path == "/status":
                return self._json(200, mirror.status())
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                return self._json(411 if length <= 0 else 413,
                                  {"error": "bad length"})
            raw = self.rfile.read(length)

            if path == "/mcp":
                if not self._mcp_authed():
                    return self._json(401, {"error": "bad bearer token"})
                return self.mcp(raw)

            if not self._app_authed():
                return self._json(401, {"error": "bad or missing token"})
            if path == "/ping":
                return self._json(200, {"status": "ok", "server": "marrow"})
            if path == "/ingest":
                try:
                    payload = json.loads(raw)
                    assert isinstance(payload, dict)
                except Exception:
                    return self._json(400, {"error": "body must be JSON object"})
                try:
                    return self._json(200, mirror.ingest(payload))
                except ValueError as exc:
                    # Malformed shape = client bug: 4xx so a broken payload
                    # can't wedge the app's outbox in an infinite retry.
                    return self._json(400, {"error": str(exc)})
            return self._json(404, {"error": "not found"})

        def mcp(self, raw):
            try:
                msg = json.loads(raw)
            except Exception:
                return self._json(400, {"error": "not JSON"})
            method = msg.get("method", "")
            if "id" not in msg:
                return self._json(202)
            rid = msg["id"]
            params = msg.get("params") or {}
            try:
                if method == "initialize":
                    result = {"protocolVersion": PROTOCOL_VERSION,
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "marrow-selfhost",
                                             "title": "Marrow (self-hosted)",
                                             "version": "1.0"}}
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    payload = call_tool(mirror, params.get("name", ""),
                                        params.get("arguments") or {})
                    result = {"content": [{"type": "text",
                                           "text": json.dumps(payload,
                                                              sort_keys=True)}],
                              "isError": False}
                else:
                    return self._json(200, {"jsonrpc": "2.0", "id": rid,
                                            "error": {"code": -32601,
                                                      "message": f"no {method}"}})
            except Exception as e:
                result = {"content": [{"type": "text", "text": f"Error: {e}"}],
                          "isError": True}
            return self._json(200, {"jsonrpc": "2.0", "id": rid,
                                    "result": result})

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--data", default="./data",
                    help="data directory (mirror + tokens)")
    ap.add_argument("--url", default=None,
                    help="advertised base URL for pairing "
                         "(default http://<lan-ip>:<port>)")
    args = ap.parse_args()

    data = Path(args.data).resolve()
    data.mkdir(parents=True, exist_ok=True)
    app_token = load_token(data / ".app-token")
    mcp_token = load_token(data / ".mcp-token")
    mirror = Mirror(data)
    base = args.url or f"http://{lan_ip()}:{args.port}"
    pairing = {"url": base, "token": app_token, "name": "Self-hosted server"}

    print(f"Marrow self-hosted server on 0.0.0.0:{args.port}")
    print(f"  pair:   {base}/pair?token={app_token}")
    print(f"  agents: {base}/mcp   (Authorization: Bearer {mcp_token})")
    ThreadingHTTPServer(("0.0.0.0", args.port),
                        make_handler(mirror, app_token, mcp_token,
                                     pairing)).serve_forever()


if __name__ == "__main__":
    main()
