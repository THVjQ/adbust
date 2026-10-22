"""adbust web: serves the site and the shared bad-app database.

The phone never talks to this server. The browser talks to the phone over
WebUSB; this only hands out the page and keeps the list of apps that people
caught popping up and removed.

    python3 web/server.py            # http://localhost:8017
    ADBUST_PORT=8017 ADBUST_DATA=/data python3 web/server.py

Standard library only.
"""

import json
import os
import re
import sqlite3
import threading
import sys
import time
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA = Path(os.environ.get("ADBUST_DATA", HERE / "data"))
PORT = int(os.environ.get("ADBUST_PORT", "8017"))

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# A phone is a hash of its serial, made in the browser; the serial never leaves.
PHONE_RE = re.compile(r"^[0-9a-f]{16}$")

# Never accept reports for these, whatever the browser says: a poisoned
# list must not be able to tell the next person to remove core system apps.
PROTECTED_PREFIXES = (
    "android", "com.android.", "com.google.android.gms",
    "com.google.android.gsf", "com.google.android.packageinstaller",
    "com.google.android.permissioncontroller", "com.google.android.webview",
    "com.samsung.android.", "com.sec.android.", "com.qualcomm.",
    "com.mediatek.", "com.miui.", "com.xiaomi.", "com.oppo.", "com.oplus.",
    "com.coloros.", "com.vivo.", "com.motorola.", "com.oneplus.",
    "com.transsion.", "com.heytap.", "com.honor.", "com.hihonor.",
)

# One browser can report at most this many apps a day; one IP, a few times that.
MAX_PER_CLIENT_DAY = 200
MAX_PER_IP_DAY = 600
MAX_BODY = 64 * 1024

# Usage events the page may send. No IPs are kept for these; the client id is
# a random per-browser id, so "people" means "different browsers".
USAGE_EVENTS = ("visit", "connect", "remove", "protect", "sort")
MAX_USAGE_PER_CLIENT_DAY = 50


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS apps (
                    pkg TEXT PRIMARY KEY,
                    label TEXT NOT NULL DEFAULT '',
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reports (
                    pkg TEXT NOT NULL,
                    client TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    how TEXT NOT NULL,
                    at TEXT NOT NULL,
                    PRIMARY KEY (pkg, client)
                );
                CREATE INDEX IF NOT EXISTS reports_at ON reports(at);
                CREATE TABLE IF NOT EXISTS usage (
                    event TEXT NOT NULL,
                    client TEXT NOT NULL,
                    at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS usage_at ON usage(at);
                CREATE TABLE IF NOT EXISTS good (
                    pkg TEXT NOT NULL,
                    client TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    at TEXT NOT NULL,
                    PRIMARY KEY (pkg, client)
                );
            """)
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(usage)")]
            if "phone" not in cols:  # added 2026-10-22
                self.conn.execute("ALTER TABLE usage ADD COLUMN phone "
                                  "TEXT NOT NULL DEFAULT ''")

    def badapps(self):
        """The shared list, in the same shape as the repo's badapps.json so
        the desktop adbust can sync from it (ADBUST_DB_URL)."""
        with self.lock:
            rows = self.conn.execute("""
                SELECT a.pkg, a.label, a.first_seen, a.last_seen,
                       COUNT(r.client) AS reports
                FROM apps a JOIN reports r ON r.pkg = a.pkg
                WHERE a.pkg NOT IN (SELECT pkg FROM good)
                GROUP BY a.pkg ORDER BY reports DESC, a.pkg
            """).fetchall()
        return {
            "schema": 1,
            "updated": _now()[:10],
            "apps": [{
                "pkg": r["pkg"], "label": r["label"],
                "category": "popup ads", "action": "uninstall",
                "notes": "caught popping up and removed",
                "added": r["first_seen"][:10], "last_seen": r["last_seen"][:10],
                "added_at": r["first_seen"] + "Z",
                "reports": r["reports"],
            } for r in rows],
        }

    def report(self, items, client, ip):
        """Record removals. Each browser counts once per app, so reports is
        the number of different people who removed it."""
        day_ago = datetime.fromtimestamp(time.time() - 86400, timezone.utc) \
            .strftime("%Y-%m-%d %H:%M:%S")
        now = _now()
        accepted = 0
        with self.lock, self.conn:
            by_client = self.conn.execute(
                "SELECT COUNT(*) FROM reports WHERE client=? AND at>?",
                (client, day_ago)).fetchone()[0]
            by_ip = self.conn.execute(
                "SELECT COUNT(*) FROM reports WHERE ip=? AND at>?",
                (ip, day_ago)).fetchone()[0]
            for it in items:
                if by_client >= MAX_PER_CLIENT_DAY or by_ip >= MAX_PER_IP_DAY:
                    break
                pkg = str(it.get("pkg") or "")
                if not PKG_RE.match(pkg) or len(pkg) > 200 or \
                        pkg.startswith(PROTECTED_PREFIXES):
                    continue
                label = str(it.get("label") or "")[:120]
                how = it.get("how") if it.get("how") in ("popup", "manual") \
                    else "database"
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO reports (pkg,client,ip,how,at) "
                    "VALUES (?,?,?,?,?)", (pkg, client, ip, how, now))
                self.conn.execute(
                    "INSERT INTO apps (pkg,label,first_seen,last_seen) "
                    "VALUES (?,?,?,?) ON CONFLICT(pkg) DO UPDATE SET "
                    "last_seen=excluded.last_seen, label=CASE WHEN "
                    "excluded.label!='' THEN excluded.label ELSE label END",
                    (pkg, label, now, now))
                if cur.rowcount:
                    accepted += 1
                    by_client += 1
                    by_ip += 1
        return accepted

    def goodapps(self):
        """Database apps someone sorted as good. They drop out of badapps(),
        so no phone gets them flagged or pre-ticked for removal any more."""
        with self.lock:
            rows = self.conn.execute("""
                SELECT g.pkg, COALESCE(a.label, MAX(g.label)) AS label,
                       a.first_seen, MIN(g.at) AS marked,
                       (SELECT COUNT(*) FROM reports r WHERE r.pkg = g.pkg)
                         AS reports
                FROM good g LEFT JOIN apps a ON a.pkg = g.pkg
                GROUP BY g.pkg ORDER BY g.pkg
            """).fetchall()
        return {"apps": [{
            "pkg": r["pkg"], "label": r["label"] or "", "reports": r["reports"],
            "added_at": (r["first_seen"] or r["marked"]) + "Z",
            "good_at": r["marked"] + "Z",
        } for r in rows]}

    def mark_good(self, items, client):
        """{"pkg", "good": true} marks an app good; "good": false puts it
        back on the bad list (clearing every good mark, not just this
        browser's)."""
        day_ago = datetime.fromtimestamp(time.time() - 86400, timezone.utc) \
            .strftime("%Y-%m-%d %H:%M:%S")
        now = _now()
        accepted = 0
        with self.lock, self.conn:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM good WHERE client=? AND at>?",
                (client, day_ago)).fetchone()[0]
            for it in items:
                pkg = str(it.get("pkg") or "")
                if not PKG_RE.match(pkg) or len(pkg) > 200:
                    continue
                if it.get("good") is False:
                    cur = self.conn.execute("DELETE FROM good WHERE pkg=?",
                                            (pkg,))
                else:
                    if n >= MAX_PER_CLIENT_DAY:
                        continue
                    cur = self.conn.execute(
                        "INSERT OR IGNORE INTO good (pkg,client,label,at) "
                        "VALUES (?,?,?,?)",
                        (pkg, client, str(it.get("label") or "")[:120], now))
                    n += cur.rowcount
                accepted += 1 if cur.rowcount else 0
        return accepted

    def track(self, event, client, phone=""):
        """Count one use of the site. Returns False if it was dropped."""
        if event not in USAGE_EVENTS:
            return False
        if not PHONE_RE.match(phone):
            phone = ""
        day_ago = datetime.fromtimestamp(time.time() - 86400, timezone.utc) \
            .strftime("%Y-%m-%d %H:%M:%S")
        with self.lock, self.conn:
            n = self.conn.execute(
                "SELECT COUNT(*) FROM usage WHERE client=? AND at>?",
                (client, day_ago)).fetchone()[0]
            if n >= MAX_USAGE_PER_CLIENT_DAY:
                return False
            self.conn.execute("INSERT INTO usage (event,client,phone,at) "
                              "VALUES (?,?,?,?)", (event, client, phone, _now()))
        return True

    def stats(self, day_start=None):
        """Totals for the sorter's counter. Phones are only known from
        2026-10-22 on, when the page started sending a phone id; visits
        from 2026-10-21. `day_start` is the viewer's local midnight in UTC
        ("YYYY-MM-DD HH:MM:SS"), for the "today" figure."""
        with self.lock:
            q = lambda sql, *a: self.conn.execute(sql, a).fetchone()[0]
            return {
                "visits": q("SELECT COUNT(*) FROM usage WHERE event='visit'"),
                "visitors": q("SELECT COUNT(DISTINCT client) FROM usage "
                              "WHERE event='visit'"),
                "visits_today": q("SELECT COUNT(*) FROM usage WHERE "
                                  "event='visit' AND at>=?", day_start)
                                if day_start else None,
                "visits_since": (q("SELECT MIN(at) FROM usage WHERE "
                                   "event='visit'") or _now())[:10],
                "phones": q("SELECT COUNT(DISTINCT phone) FROM usage "
                            "WHERE phone!='' AND event='connect'"),
                "phones_cleaned": q("SELECT COUNT(DISTINCT phone) FROM usage "
                                    "WHERE phone!='' AND event='remove'"),
                "removals": q("SELECT COUNT(*) FROM reports"),
                "since": (q("SELECT MIN(at) FROM usage WHERE phone!=''")
                          or _now())[:10],
            }

    def usage(self, days=7, tz=timezone.utc):
        """Per-day totals for the last `days` days, newest first, with days
        cut at midnight in `tz`. Each event: [times, different browsers]."""
        start = (datetime.now(tz) - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        since = start.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self.lock:
            rows = self.conn.execute(
                "SELECT event, client, at FROM usage WHERE at>=?",
                (since,)).fetchall()
            added = self.conn.execute(
                "SELECT first_seen FROM apps WHERE first_seen>=?",
                (since,)).fetchall()
        out = {}
        local = lambda at: datetime.strptime(at, "%Y-%m-%d %H:%M:%S") \
            .replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m-%d")
        for r in rows:
            d = out.setdefault(local(r["at"]), {})
            d.setdefault(r["event"], []).append(r["client"])
        for r in added:
            out.setdefault(local(r["first_seen"]), {}) \
                .setdefault("new_apps", []).append(None)
        return {day: {ev: [len(cs), len(set(cs) - {None})]
                      for ev, cs in evs.items()}
                for day, evs in sorted(out.items(), reverse=True)}


class Handler(SimpleHTTPRequestHandler):
    store = None

    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(STATIC), **k)

    def log_message(self, fmt, *args):
        if os.environ.get("ADBUST_QUIET") != "1":
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The page must be allowed to use WebUSB, and nothing else.
        self.send_header("Permissions-Policy", "usb=(self)")
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
        elif self.path.split("?", 1)[0].endswith((".js", ".css", ".html", "/")):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def _json(self, obj, status=HTTPStatus.OK):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _ip(self):
        return (self.headers.get("CF-Connecting-IP")
                or self.client_address[0])[:64]

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/api/badapps", "/badapps.json"):
            return self._json(self.store.badapps())
        if path == "/api/stats":
            m = re.search(r"[?&]today=(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z",
                          self.path)
            return self._json(self.store.stats(
                m.group(1).replace("T", " ") if m else None))
        if path == "/api/goodapps":
            return self._json(self.store.goodapps())
        if path == "/api/health":
            return self._json({"ok": True})
        return super().do_GET()

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/api/usage":
            return self._usage()
        if path not in ("/api/removed", "/api/good"):
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise ValueError("bad length")
            doc = json.loads(self.rfile.read(n))
            client = str(doc.get("client") or "")
            items = doc.get("apps")
            if not CLIENT_RE.match(client) or not isinstance(items, list):
                raise ValueError("bad body")
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "bad request"},
                              HTTPStatus.BAD_REQUEST)
        items = [i for i in items[:100] if isinstance(i, dict)]
        if path == "/api/good":
            return self._json({"accepted": self.store.mark_good(items, client)})
        accepted = self.store.report(items, client, self._ip())
        return self._json({"accepted": accepted})

    def _usage(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > 1024:
                raise ValueError("bad length")
            doc = json.loads(self.rfile.read(n))
            client = str(doc.get("client") or "")
            if not CLIENT_RE.match(client):
                raise ValueError("bad body")
        except (ValueError, json.JSONDecodeError, AttributeError):
            return self._json({"error": "bad request"},
                              HTTPStatus.BAD_REQUEST)
        return self._json({"ok": self.store.track(
            str(doc.get("event")), client, str(doc.get("phone") or ""))})


def print_usage(days):
    """`server.py --usage [days]`: the usage table, days in Sydney time.
    Run it on the host as the apps user — the image has no tz database:
      sudo -u apps ADBUST_DATA=/mnt/Pool1-MAIN/apps/adbust-web/data \\
        python3 /mnt/Pool1-MAIN/apps/adbust-web/src/server.py --usage"""
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(os.environ.get("ADBUST_TZ", "Australia/Sydney"))
    except Exception:
        tz = timezone.utc
    rep = Store(DATA / "adbust-web.db").usage(days, tz)
    cols = USAGE_EVENTS + ("new_apps",)
    print(f"{'day':<12}" + "".join(f"{c:>14}" for c in cols)
          + f"   (times/browsers, {tz})")
    for day, evs in rep.items():
        cells = ["%d/%d" % tuple(evs[c]) if c in evs else "-"
                 for c in USAGE_EVENTS]
        cells.append(str(evs["new_apps"][0]) if "new_apps" in evs else "-")
        print(f"{day:<12}" + "".join(f"{c:>14}" for c in cells))


def main():
    if sys.argv[1:2] == ["--usage"]:
        return print_usage(int(sys.argv[2]) if sys.argv[2:] else 7)
    Handler.store = Store(DATA / "adbust-web.db")
    Handler.extensions_map = {**Handler.extensions_map,
                              ".js": "text/javascript",
                              ".dex": "application/octet-stream"}
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"adbust web on http://localhost:{PORT}  (data: {DATA})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
