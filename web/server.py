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
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"
DATA = Path(os.environ.get("ADBUST_DATA", HERE / "data"))
PORT = int(os.environ.get("ADBUST_PORT", "8017"))

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
CLIENT_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

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
            """)

    def badapps(self):
        """The shared list, in the same shape as the repo's badapps.json so
        the desktop adbust can sync from it (ADBUST_DB_URL)."""
        with self.lock:
            rows = self.conn.execute("""
                SELECT a.pkg, a.label, a.first_seen, a.last_seen,
                       COUNT(r.client) AS reports
                FROM apps a JOIN reports r ON r.pkg = a.pkg
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
                how = "popup" if it.get("how") == "popup" else "database"
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
        elif self.path.endswith((".js", ".html", "/")):
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
        if path == "/api/health":
            return self._json({"ok": True})
        return super().do_GET()

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/removed":
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
        accepted = self.store.report(items, client, self._ip())
        return self._json({"accepted": accepted})


def main():
    Handler.store = Store(DATA / "adbust-web.db")
    Handler.extensions_map = {**Handler.extensions_map,
                              ".js": "text/javascript",
                              ".dex": "application/octet-stream"}
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"adbust web on http://localhost:{PORT}  (data: {DATA})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
