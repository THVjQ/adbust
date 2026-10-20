#!/usr/bin/env python3
"""
adbust - Android popup/adware triage for a repair bench.

Finds the app throwing full-screen ads and strips its ability to do it, and
keeps a database of known-bad apps so the next handset is faster.
Runs on Windows and Linux. Needs adb; the handset needs USB debugging on.

    python adbust.py            point-and-click window (default)
    python adbust.py scan       score user apps for popup capability
    python adbust.py watch      live-name the app the moment an ad appears
    python adbust.py fix        interactive per-app triage
    python adbust.py clean      remove every app that is in the bad-app database
    python adbust.py nuke       bulk-declaw everything above a score
    python adbust.py report     write a summary for the job card
    python adbust.py db ...     list / add / remove / export / update the database

No third-party packages. The GUI uses tkinter from the standard library
(Fedora: sudo dnf install python3-tkinter).
"""

import argparse
import json
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

__version__ = "2.0.0"

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""
HERE = Path(__file__).resolve().parent


# ============================================================ theme

# Kigga palette
BLUE = "#73AFCE"
NAVY = "#213D4F"
NAVY_DEEP = "#18303E"
NAVY_PANEL = "#2C4E63"
NAVY_LINE = "#35596F"
TEXT = "#F2F7FA"
MUTED = "#9DBACB"
SEVERE = "#FF9999"
HIGH = "#FFB366"
MEDIUM = "#FFD27F"
GOOD = "#8FD9A8"
DANGER = "#E0626F"


def _enable_windows_vt():
    """Turn on ANSI escape handling in the Windows console. Harmless if it
    fails - we just fall back to plain text."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # -11 = STD_OUTPUT_HANDLE, 0x4 = ENABLE_VIRTUAL_TERMINAL_PROCESSING
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x4))
    except Exception:
        return False


def _colour_supported():
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout or not sys.stdout.isatty():
        return False
    if IS_WINDOWS:
        if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
            return True
        return _enable_windows_vt()
    return os.environ.get("TERM", "") != "dumb"


COLOUR = _colour_supported()


def _hex_to_rgb(h):
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def paint(text, hexcolour, bold=False):
    if not COLOUR:
        return text
    r, g, b = _hex_to_rgb(hexcolour)
    prefix = "\033[1m" if bold else ""
    return f"{prefix}\033[38;2;{r};{g};{b}m{text}\033[0m"


def strip_ansi(text):
    return re.sub(r"\033\[[0-9;]*m", "", text)


# ============================================================ constants

PROTECTED_PREFIXES = (
    "android",
    "com.android.",
    "com.google.android.gms",
    "com.google.android.gsf",
    "com.google.android.packageinstaller",
    "com.google.android.permissioncontroller",
    "com.google.android.webview",
    "com.samsung.android.",
    "com.sec.android.",
    "com.qualcomm.",
    "com.mediatek.",
    "com.miui.",
    "com.xiaomi.",
    "com.oppo.",
    "com.oplus.",
    "com.coloros.",
    "com.vivo.",
    "com.motorola.",
    "com.oneplus.",
    "com.transsion.",
    "com.heytap.",
    "com.honor.",
    "com.hihonor.",
)

SUSPECT_WORDS = (
    "pdf", "scanner", "cleaner", "booster", "speedup", "ramclean", "junk",
    "flashlight", "torch", "wallpaper", "qrcode", "qrscan", "barcode",
    "battery", "antivirus", "security", "vpnfree", "freevpn", "photoeditor",
    "videoeditor", "keyboardtheme", "callrecorder", "weatherpro",
    "filemanager", "musicplayer", "videoplayer", "compass", "translator",
    "screenrecord", "documentreader", "allreader", "officereader",
)

# Fragments of activity class names that belong to ad SDKs. When the window
# that just took the screen is one of these, the package that owns it is the
# app showing the ad.
AD_ACTIVITY_MARKERS = (
    "adactivity", "interstitial", "admob", "gms.ads", "facebook.ads",
    "audiencenetwork", "applovin", "unity3d.ads", "ironsource", "mbridge",
    "mintegral", "vungle", "chartboost", "inmobi", "startapp", "pangle",
    "openadsdk", "fyber", "adcolony", "appodeal", "yandex.mobile.ads",
    "bigosdk", "tapjoy", "adsdk", "fullscreenad", "rewardedad",
)

# Window types that draw on top of other apps rather than as a normal screen.
OVERLAY_WINDOW_TYPES = (
    "APPLICATION_OVERLAY", "SYSTEM_ALERT", "SYSTEM_OVERLAY", "PHONE",
    "PRIORITY_PHONE", "SYSTEM_ERROR",
)

TRUSTED_INSTALLERS = ("com.android.vending", "com.google.android.feedback")

SEVERITY = [
    (12, "SEVERE", SEVERE),
    (8, "HIGH", HIGH),
    (5, "MEDIUM", MEDIUM),
    (3, "LOW", BLUE),
    (0, "clean", MUTED),
]

# Score added for an app already in the bad-app database. Large enough that a
# database hit is always SEVERE on its own.
KNOWN_BAD_SCORE = 12

RECENT_DAYS = 14

PKG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")


def severity_of(score):
    for threshold, name, colour in SEVERITY:
        if score >= threshold:
            return name, colour
    return "clean", MUTED


def is_protected(pkg):
    return pkg.startswith(PROTECTED_PREFIXES)


def valid_pkg(pkg):
    return bool(PKG_RE.match(pkg or ""))


def ad_like_activity(activity):
    a = (activity or "").lower()
    return any(m in a for m in AD_ACTIVITY_MARKERS)


# ============================================================ bad-app database

SHARED_DB_FILE = HERE / "badapps.json"
DEFAULT_DB_URL = os.environ.get(
    "ADBUST_DB_URL",
    "https://raw.githubusercontent.com/THVjQ/adbust/main/badapps.json")
DB_SCHEMA = 1

CATEGORIES = ("adware", "popup ads", "fake cleaner / booster", "malware",
              "stalkerware", "junk / bloat", "other")
ACTIONS = ("uninstall", "disable", "declaw")


def data_dir():
    if os.environ.get("ADBUST_HOME"):
        return Path(os.environ["ADBUST_HOME"])
    if IS_WINDOWS:
        return Path(os.environ.get("APPDATA") or Path.home()) / "adbust"
    base = os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return Path(base) / "adbust"


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DBError(Exception):
    pass


class BadAppDB:
    """The bench's memory: known-bad apps plus a history of every job.

    Two layers. `badapps.json` beside this script is the *shared* list - it
    lives in the git repo, so everyone who pulls gets it, and "Update" pulls
    the newest copy from GitHub (or, later, from a server speaking the same
    JSON). The SQLite file in the user data dir holds this bench's own
    additions, how often each app was seen and removed, and the job history.
    "Export" folds local additions back into badapps.json for a commit."""

    def __init__(self, path=None, shared_file=SHARED_DB_FILE):
        self.path = Path(path) if path else data_dir() / "adbust.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.shared_file = Path(shared_file) if shared_file else None
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock, self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS bad_apps (
                    pkg TEXT PRIMARY KEY,
                    label TEXT DEFAULT '',
                    category TEXT DEFAULT 'adware',
                    action TEXT DEFAULT 'uninstall',
                    notes TEXT DEFAULT '',
                    source TEXT DEFAULT 'local',
                    added TEXT,
                    reports INTEGER DEFAULT 1,
                    times_seen INTEGER DEFAULT 0,
                    times_removed INTEGER DEFAULT 0,
                    last_seen TEXT
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started TEXT, model TEXT, serial TEXT, android TEXT,
                    bench TEXT
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER, ts TEXT, pkg TEXT, action TEXT,
                    result TEXT
                );
            """)
        if self.shared_file and self.shared_file.is_file():
            try:
                self.merge(load_db_json(self.shared_file.read_text()),
                           source="shared")
            except DBError:
                pass  # a broken shared file must not stop the bench working

    def close(self):
        with self.lock:
            self.conn.close()

    # -- bad apps -------------------------------------------------

    def get(self, pkg):
        with self.lock:
            row = self.conn.execute("SELECT * FROM bad_apps WHERE pkg=?",
                                    (pkg,)).fetchone()
        return dict(row) if row else None

    def all(self, search=""):
        with self.lock:
            if search:
                like = f"%{search}%"
                rows = self.conn.execute(
                    "SELECT * FROM bad_apps WHERE pkg LIKE ? OR label LIKE ? "
                    "OR category LIKE ? OR notes LIKE ? ORDER BY pkg",
                    (like, like, like, like)).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT * FROM bad_apps ORDER BY pkg").fetchall()
        return [dict(r) for r in rows]

    def known(self, pkgs):
        """{pkg: row} for those of `pkgs` that are in the database."""
        found = {}
        pkgs = list(pkgs)
        with self.lock:
            for i in range(0, len(pkgs), 400):
                chunk = pkgs[i:i + 400]
                marks = ",".join("?" * len(chunk))
                for r in self.conn.execute(
                        f"SELECT * FROM bad_apps WHERE pkg IN ({marks})",
                        chunk):
                    found[r["pkg"]] = dict(r)
        return found

    def add(self, pkg, label="", category="adware", action="uninstall",
            notes="", source="local"):
        pkg = pkg.strip()
        if not valid_pkg(pkg):
            raise DBError(f"'{pkg}' is not a valid package name")
        if category not in CATEGORIES:
            category = "other"
        if action not in ACTIONS:
            action = "uninstall"
        with self.lock, self.conn:
            existing = self.get(pkg)
            if existing:
                self.conn.execute(
                    "UPDATE bad_apps SET label=?, category=?, action=?, "
                    "notes=?, source=CASE WHEN source='shared' THEN "
                    "'shared+local' ELSE source END WHERE pkg=?",
                    (label, category, action, notes, pkg))
            else:
                self.conn.execute(
                    "INSERT INTO bad_apps (pkg,label,category,action,notes,"
                    "source,added) VALUES (?,?,?,?,?,?,?)",
                    (pkg, label, category, action, notes, source, _today()))
        return self.get(pkg)

    def remove(self, pkg):
        with self.lock, self.conn:
            cur = self.conn.execute("DELETE FROM bad_apps WHERE pkg=?",
                                    (pkg,))
        return cur.rowcount > 0

    def mark_seen(self, pkgs):
        with self.lock, self.conn:
            for p in pkgs:
                self.conn.execute(
                    "UPDATE bad_apps SET times_seen=times_seen+1, "
                    "last_seen=? WHERE pkg=?", (_today(), p))

    def mark_removed(self, pkg):
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE bad_apps SET times_removed=times_removed+1 "
                "WHERE pkg=?", (pkg,))

    def merge(self, apps, source="shared"):
        """Fold a list of app dicts in. Rows this bench edited keep their
        local text; everything else takes the incoming values.
        Returns (added, updated)."""
        added = updated = 0
        with self.lock, self.conn:
            for a in apps:
                pkg = (a.get("pkg") or "").strip()
                if not valid_pkg(pkg):
                    continue
                cat = a.get("category") if a.get("category") in CATEGORIES \
                    else "other"
                act = a.get("action") if a.get("action") in ACTIONS \
                    else "uninstall"
                try:
                    reports = int(a.get("reports") or 1)
                except (TypeError, ValueError):
                    reports = 1
                label, notes = a.get("label") or "", a.get("notes") or ""
                row = self.get(pkg)
                if row is None:
                    self.conn.execute(
                        "INSERT INTO bad_apps (pkg,label,category,action,"
                        "notes,source,added,reports) VALUES (?,?,?,?,?,?,?,?)",
                        (pkg, label, cat, act, notes, source,
                         a.get("added") or _today(), reports))
                    added += 1
                elif row["source"] == "shared":
                    if (row["label"], row["category"], row["action"],
                            row["notes"], row["reports"]) != (
                            label, cat, act, notes, reports):
                        self.conn.execute(
                            "UPDATE bad_apps SET label=?, category=?, "
                            "action=?, notes=?, reports=? WHERE pkg=?",
                            (label, cat, act, notes, reports, pkg))
                        updated += 1
                elif row["source"] == "local":
                    # keep the bench's text, but remember the row now also
                    # exists upstream
                    self.conn.execute(
                        "UPDATE bad_apps SET source='shared+local', "
                        "reports=MAX(reports, ?) WHERE pkg=?",
                        (reports, pkg))
        return added, updated

    def export_json(self, path=None):
        """Write every known-bad app to the shared JSON (default:
        badapps.json beside the script). Returns the number written."""
        path = Path(path) if path else self.shared_file
        apps = []
        for r in self.all():
            apps.append({
                "pkg": r["pkg"],
                "label": r["label"] or "",
                "category": r["category"],
                "action": r["action"],
                "notes": r["notes"] or "",
                "added": r["added"] or _today(),
                "reports": max(int(r["reports"] or 1),
                               int(r["times_seen"] or 0)),
            })
        doc = {"schema": DB_SCHEMA, "updated": _today(), "apps": apps}
        Path(path).write_text(json.dumps(doc, indent=2) + "\n")
        return len(apps)

    def import_file(self, path):
        return self.merge(load_db_json(Path(path).read_text()),
                          source="shared")

    def update_from_url(self, url=None, timeout=15):
        url = url or DEFAULT_DB_URL
        req = urllib.request.Request(
            url, headers={"User-Agent": f"adbust/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                text = resp.read().decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - network errors vary a lot
            raise DBError(f"could not download {url}: {exc}")
        return self.merge(load_db_json(text), source="shared")

    # -- jobs and history -----------------------------------------

    def start_job(self, device):
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO jobs (started,model,serial,android,bench) "
                "VALUES (?,?,?,?,?)",
                (_now(), device.model, device.serial,
                 f"{device.release} (API {device.sdk})",
                 f"{platform.system()} {platform.node()}"))
        return cur.lastrowid

    def log_action(self, job_id, pkg, action, result):
        with self.lock, self.conn:
            self.conn.execute(
                "INSERT INTO actions (job_id,ts,pkg,action,result) "
                "VALUES (?,?,?,?,?)", (job_id, _now(), pkg, action, result))

    def jobs(self, limit=200):
        with self.lock:
            rows = self.conn.execute(
                "SELECT j.*, COUNT(a.id) AS n_actions FROM jobs j "
                "LEFT JOIN actions a ON a.job_id=j.id GROUP BY j.id "
                "ORDER BY j.id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def actions_for(self, job_id):
        with self.lock:
            rows = self.conn.execute(
                "SELECT * FROM actions WHERE job_id=? ORDER BY id",
                (job_id,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self):
        with self.lock:
            c = self.conn.execute
            return {
                "apps": c("SELECT COUNT(*) FROM bad_apps").fetchone()[0],
                "local": c("SELECT COUNT(*) FROM bad_apps WHERE source "
                           "!= 'shared'").fetchone()[0],
                "jobs": c("SELECT COUNT(*) FROM jobs").fetchone()[0],
                "removed": c("SELECT COUNT(*) FROM actions WHERE action IN "
                             "('uninstall','disable')").fetchone()[0],
            }


def load_db_json(text):
    try:
        doc = json.loads(text)
    except ValueError as exc:
        raise DBError(f"not valid JSON: {exc}")
    apps = doc.get("apps") if isinstance(doc, dict) else doc
    if not isinstance(apps, list):
        raise DBError("expected an object with an 'apps' list")
    return [a for a in apps if isinstance(a, dict)]


# ============================================================ adb discovery

class AdbError(Exception):
    pass


def _candidate_adb_paths():
    out = [HERE / f"adb{EXE}", HERE / "platform-tools" / f"adb{EXE}"]
    if os.environ.get("ANDROID_HOME"):
        out.append(Path(os.environ["ANDROID_HOME"]) / "platform-tools"
                   / f"adb{EXE}")
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        userp = os.environ.get("USERPROFILE", "")
        out += [
            Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
            Path(userp) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
            Path(userp) / "scoop" / "apps" / "adb" / "current" / "adb.exe",
            Path(local) / "Microsoft" / "WinGet" / "Links" / "adb.exe",
            Path(r"C:\Android\platform-tools\adb.exe"),
            Path(r"C:\platform-tools\adb.exe"),
            Path(r"C:\ProgramData\chocolatey\bin\adb.exe"),
        ]
    else:
        home = Path.home()
        out += [
            Path("/usr/bin/adb"),
            Path("/usr/local/bin/adb"),
            Path("/snap/bin/adb"),
            Path("/opt/android-sdk/platform-tools/adb"),
            home / "Android" / "Sdk" / "platform-tools" / "adb",
            home / ".local" / "bin" / "adb",
        ]
    return out


def _linux_distro_id():
    try:
        text = Path("/etc/os-release").read_text()
    except OSError:
        return ""
    ids = []
    for line in text.splitlines():
        if line.startswith("ID="):
            ids.append(line.split("=", 1)[1].strip().strip('"'))
        elif line.startswith("ID_LIKE="):
            ids += line.split("=", 1)[1].strip().strip('"').split()
    return " ".join(ids).lower()


def adb_install_hint():
    """Platform-correct install instructions, as plain text."""
    if IS_WINDOWS:
        return (
            "Install Google's platform-tools, then re-run:\n"
            "    winget install Google.PlatformTools\n"
            "  or with Scoop:   scoop install adb\n"
            "  or download the platform-tools zip from\n"
            "    https://developer.android.com/tools/releases/platform-tools\n"
            "  and unzip it so that adb.exe sits next to this script."
        )
    distro = _linux_distro_id()
    if "fedora" in distro or "rhel" in distro:
        cmd = "sudo dnf install android-tools"
    elif "debian" in distro or "ubuntu" in distro:
        cmd = "sudo apt install android-tools-adb"
    elif "arch" in distro:
        cmd = "sudo pacman -S android-tools"
    elif "suse" in distro:
        cmd = "sudo zypper install android-tools"
    else:
        cmd = "install your distro's android-tools / adb package"
    return (
        f"Install adb, then re-run:\n    {cmd}\n"
        "  Then add yourself to the plugdev group if the device shows as "
        "'no permissions'."
    )


def find_adb():
    found = shutil.which("adb")
    if found:
        return found
    for path in _candidate_adb_paths():
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        except OSError:
            continue
    return None


def _popen_flags():
    return subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0


# ============================================================ device

def _parse_focus(text):
    """(package, activity) from the focus lines of a dumpsys window/activity
    dump, or (None, None)."""
    for line in text.splitlines():
        if not any(k in line for k in
                   ("mCurrentFocus", "mFocusedApp", "mResumedActivity",
                    "topResumedActivity")):
            continue
        m = re.search(r"([a-zA-Z][a-zA-Z0-9_.]+)/([a-zA-Z0-9_.$]+)", line)
        if m and m.group(1) not in ("null", "Window"):
            return m.group(1), m.group(2)
    return None, None


def parse_overlays(text):
    """Packages owning a visible draw-over-other-apps window, from
    `dumpsys window windows`."""
    owners = set()
    for block in re.split(r"\n\s*Window #\d+ ", "\n" + text)[1:]:
        ty = re.search(r"\bty=([A-Z_]+)", block)
        if not ty or ty.group(1) not in OVERLAY_WINDOW_TYPES:
            continue
        if re.search(r"isVisible=false|mViewVisibility=0x[48]\b|"
                     r"isOnScreen=false|mHasSurface=false", block):
            continue
        pm = re.search(r"\bpackage=([A-Za-z][\w.]+)", block)
        if not pm:
            pm = re.match(r"Window\{\w+ u\d+ ([A-Za-z][\w.]+)", block)
        if pm:
            owners.add(pm.group(1))
    return owners


def parse_package_dump(text):
    """{pkg: {version, first_install, last_update}} from
    `dumpsys package packages`."""
    info = {}
    blocks = re.split(r"\n\s*Package \[", "\n" + text)
    for block in blocks[1:]:
        m = re.match(r"([A-Za-z][\w.]+)\]", block)
        if not m:
            continue
        pkg = m.group(1)
        if pkg in info:
            continue  # hidden-system duplicates come later in the dump

        def field(name):
            f = re.search(rf"\b{name}=([^\n]+)", block)
            return f.group(1).strip() if f else ""

        info[pkg] = {
            "version": field("versionName"),
            "first_install": field("firstInstallTime")[:16],
            "last_update": field("lastUpdateTime")[:16],
        }
    return info


class Device:
    """One handset, reached over adb. All adb calls go through here."""

    def __init__(self, adb_path, serial=None):
        self.adb_path = adb_path
        self.serial = serial
        self._focus_source = None
        self.model = ""
        self.release = ""
        self.sdk = 0

    # -- plumbing -------------------------------------------------

    def _run(self, args, timeout=40):
        cmd = [self.adb_path]
        if self.serial:
            cmd += ["-s", self.serial]
        cmd += list(args)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                errors="replace",
                creationflags=_popen_flags(),
            )
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb timed out running: {' '.join(args)}")
        except FileNotFoundError:
            raise AdbError("adb vanished from its path mid-session")
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and not proc.stdout and re.search(
                r"device .*not found|device offline|no devices|unauthorized",
                err):
            raise AdbError(f"Lost the handset: {err}")
        return (proc.stdout or "") + (err if args[:1] == ["install"] else "")

    def shell(self, *parts, timeout=40):
        """Run on the handset. Arguments are passed as separate argv entries,
        so no host-side quoting rules apply and no shell metacharacters are
        needed - this is what keeps Windows and Linux behaving identically."""
        return self._run(["shell", *[str(p) for p in parts]], timeout=timeout)

    def prop(self, name):
        return self.shell("getprop", name).strip()

    def identify(self):
        self.model = self.prop("ro.product.model") or "unknown handset"
        brand = self.prop("ro.product.brand")
        if brand and brand.lower() not in self.model.lower():
            self.model = f"{brand.capitalize()} {self.model}"
        self.release = self.prop("ro.build.version.release") or "?"
        try:
            self.sdk = int(self.prop("ro.build.version.sdk") or 0)
        except ValueError:
            self.sdk = 0
        return self

    def describe(self):
        return f"{self.model} - Android {self.release} (API {self.sdk})"

    # -- inventory ------------------------------------------------

    def third_party_packages(self):
        """{package: installer or None} for user-installed apps."""
        out = self.shell("pm", "list", "packages", "-3", "-i")
        pkgs = {}
        for line in out.splitlines():
            line = line.strip()
            m = re.match(r"package:(\S+)\s+installer=(\S+)", line)
            if m:
                inst = m.group(2)
                pkgs[m.group(1)] = None if inst in ("null", "?") else inst
                continue
            m = re.match(r"package:(\S+)", line)
            if m:
                pkgs[m.group(1)] = None
        return pkgs

    def package_details(self):
        try:
            return parse_package_dump(
                self.shell("dumpsys", "package", "packages", timeout=90))
        except AdbError:
            return {}

    def launcher_packages(self):
        """Packages with a home-screen icon. Adware often hides its own."""
        out = self.shell("cmd", "package", "query-activities", "--brief",
                         "-a", "android.intent.action.MAIN",
                         "-c", "android.intent.category.LAUNCHER")
        found = set(re.findall(r"^\s*([A-Za-z][\w.]+)/", out, re.MULTILINE))
        return found or None  # None = couldn't tell, don't score it

    def home_package(self):
        out = self.shell("cmd", "package", "resolve-activity", "--brief",
                         "-a", "android.intent.action.MAIN",
                         "-c", "android.intent.category.HOME")
        m = re.findall(r"([A-Za-z][\w.]+)/", out)
        return m[-1] if m else None

    def overlay_packages(self, known_pkgs):
        """Packages allowed to draw over other apps.

        Tries the bulk query first (one adb round trip); falls back to
        per-package checks on ROMs that don't support it."""
        out = self.shell("cmd", "appops", "query-op", "SYSTEM_ALERT_WINDOW",
                         "allow")
        lowered = out.lower()
        usable = out.strip() and not any(
            bad in lowered for bad in ("unknown command", "usage:", "exception",
                                       "error:", "no operations")
        )
        if usable:
            hits = set(re.findall(r"^\s*([a-zA-Z][a-zA-Z0-9_.]+)\s*$",
                                  out, re.MULTILINE))
            if hits:
                return {p for p in hits if p in known_pkgs}
        allowed = set()
        for pkg in known_pkgs:
            res = self.shell("cmd", "appops", "get", pkg,
                             "SYSTEM_ALERT_WINDOW", timeout=15)
            if re.search(r"SYSTEM_ALERT_WINDOW:\s*allow", res):
                allowed.add(pkg)
        return allowed

    def _secure(self, key):
        raw = self.shell("settings", "get", "secure", key).strip()
        return "" if raw in ("", "null") else raw

    def accessibility_packages(self):
        raw = self._secure("enabled_accessibility_services")
        return {c.split("/")[0] for c in raw.split(":") if "/" in c}

    def notification_listener_packages(self):
        raw = self._secure("enabled_notification_listeners")
        return {c.split("/")[0] for c in raw.split(":") if "/" in c}

    def battery_exempt_packages(self):
        out = self.shell("dumpsys", "deviceidle", "whitelist")
        pkgs = set()
        for line in out.splitlines():
            line = line.strip()
            m = re.search(r",([a-zA-Z][a-zA-Z0-9_.]+),", line)
            if m:
                pkgs.add(m.group(1))
                continue
            m = re.match(r"(?:system|user),\s*([a-zA-Z][a-zA-Z0-9_.]+)", line)
            if m:
                pkgs.add(m.group(1))
        return pkgs

    def device_admin_packages(self):
        """{package: admin component} for active device admins."""
        out = self.shell("dumpsys", "device_policy")
        admins = {}
        for pkg, comp in re.findall(
                r"ComponentInfo\{([A-Za-z][\w.]+)/([\w.$]+)\}", out):
            admins.setdefault(pkg, f"{pkg}/{comp}")
        return admins

    # -- foreground tracking --------------------------------------

    def foreground(self):
        """(package, activity) currently on screen, best effort.

        Filtering happens here rather than with a device-side grep, so this
        doesn't depend on toybox being present or well-behaved on the ROM."""
        sources = [
            ("window_displays", ("dumpsys", "window", "displays")),
            ("window", ("dumpsys", "window")),
            ("activities", ("dumpsys", "activity", "activities")),
        ]
        if self._focus_source:
            sources.sort(key=lambda s: s[0] != self._focus_source)

        for name, args in sources:
            out = self.shell(*args, timeout=20)
            if not out:
                continue
            pkg, act = _parse_focus(out)
            if pkg:
                self._focus_source = name
                return pkg, act
        return None, None

    def window_state(self, with_overlays=True):
        """(package, activity, overlay_owners) in one adb round trip where
        the ROM allows it."""
        if not with_overlays:
            pkg, act = self.foreground()
            return pkg, act, set()
        out = self.shell("dumpsys", "window", "windows", timeout=20)
        pkg, act = _parse_focus(out)
        if not pkg:
            pkg, act = self.foreground()
        return pkg, act, parse_overlays(out)

    # -- actions --------------------------------------------------

    def _strip_secure_component(self, key, pkg):
        raw = self._secure(key)
        if not raw:
            return False
        kept = [c for c in raw.split(":") if c and not c.startswith(pkg + "/")]
        new = ":".join(kept)
        if new == raw:
            return False
        if new:
            self.shell("settings", "put", "secure", key, new)
        else:
            self.shell("settings", "delete", "secure", key)
        return True

    def declaw(self, pkg):
        """Remove an app's ability to interrupt, without uninstalling it.
        Safe on a customer handset - the app and its data stay put."""
        done = []
        self.shell("cmd", "appops", "set", pkg, "SYSTEM_ALERT_WINDOW", "deny")
        done.append("overlay denied")
        self.shell("cmd", "appops", "set", pkg, "POST_NOTIFICATION", "ignore")
        done.append("notifications blocked")
        self.shell("cmd", "appops", "set", pkg, "RUN_IN_BACKGROUND", "ignore")
        self.shell("cmd", "appops", "set", pkg, "START_FOREGROUND", "deny")
        done.append("background starts blocked")
        if self._strip_secure_component("enabled_accessibility_services", pkg):
            done.append("accessibility revoked")
        if self._strip_secure_component("enabled_notification_listeners", pkg):
            done.append("notification access revoked")
        self.shell("dumpsys", "deviceidle", "whitelist", f"-{pkg}")
        self.shell("am", "force-stop", pkg)
        done.append("force-stopped")
        return done

    def force_stop(self, pkg):
        self.shell("am", "force-stop", pkg)
        return "force-stopped"

    def disable(self, pkg):
        return self.shell("pm", "disable-user", "--user", "0", pkg).strip() \
            or "disabled"

    def enable(self, pkg):
        return self.shell("pm", "enable", "--user", "0", pkg).strip() \
            or "enabled"

    def remove_admin(self, component):
        return self.shell("dpm", "remove-active-admin", component).strip()

    def uninstall(self, pkg, admin_component=None):
        """Uninstall for the current user. An active device admin blocks
        uninstall, so try to drop admin first; dpm only allows that for
        test-only admins, so the fallback is opening App info on the phone."""
        if admin_component:
            self.remove_admin(admin_component)
        out = self.shell("pm", "uninstall", "--user", "0", pkg).strip() \
            or "removed"
        if "DEVICE_POLICY" in out or "DELETE_FAILED" in out:
            self.open_app_info(pkg)
            out += (" - it is a device admin: App info is now open on the "
                    "phone, deactivate admin there, then uninstall again")
        return out

    def is_installed(self, pkg):
        out = self.shell("pm", "list", "packages", pkg)
        return f"package:{pkg}" in out.split()

    def open_app_info(self, pkg):
        self.shell("am", "start", "-a",
                   "android.settings.APPLICATION_DETAILS_SETTINGS",
                   "-d", f"package:{pkg}")
        return "App info opened on the phone"

    def install(self, apk_path):
        out = self._run(["install", "-r", str(apk_path)], timeout=300)
        return out.strip().splitlines()[-1] if out.strip() else "no output"


def list_devices(adb_path):
    """[(serial, state, description)] for everything attached."""
    try:
        proc = subprocess.run(
            [adb_path, "devices", "-l"],
            capture_output=True, text=True, timeout=20, check=False,
            errors="replace", creationflags=_popen_flags(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise AdbError(f"could not run adb: {exc}")

    found = []
    for line in (proc.stdout or "").splitlines()[1:]:
        line = line.rstrip()
        if not line.strip() or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        model = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":", 1)[1].replace("_", " ")
        found.append((serial, state, model or serial))
    return found


def pick_device(adb_path, serial=None, chooser=None):
    """Resolve to one usable Device, or raise AdbError with advice."""
    devices = list_devices(adb_path)
    usable = [d for d in devices if d[1] == "device"]
    unauth = [d for d in devices if d[1] == "unauthorized"]
    offline = [d for d in devices if d[1] == "offline"]
    noperm = [d for d in devices if d[1] == "no" or "permission" in d[1]]

    if serial:
        if serial not in [d[0] for d in usable]:
            raise AdbError(f"{serial} is not attached and ready.")
        return Device(adb_path, serial).identify()

    if not usable:
        if unauth or offline:
            raise AdbError(
                "Handset is connected but not authorised yet.\n"
                "  Unlock the phone and tap 'Allow USB debugging' (tick "
                "'Always allow'),\n  then press Connect again. No prompt? "
                "Unplug, revoke USB debugging\n  authorisations in Developer "
                "options, and replug.")
        if noperm:
            raise AdbError(
                "Device seen but not accessible.\n"
                + ("  Try a different USB port or cable, and check the "
                   "driver in Device Manager." if IS_WINDOWS else
                   "  Usually a udev rule - add yourself to plugdev and "
                   "replug."))
        raise AdbError(
            "No device detected.\n"
            "  Check: USB debugging enabled, cable carries data (not "
            "charge-only),\n"
            "  and the USB mode on the phone is set to file transfer.")

    env_serial = os.environ.get("ANDROID_SERIAL")
    if env_serial and env_serial in [d[0] for d in usable]:
        return Device(adb_path, env_serial).identify()

    if len(usable) == 1:
        return Device(adb_path, usable[0][0]).identify()

    if chooser:
        chosen = chooser(usable)
        if chosen:
            return Device(adb_path, chosen).identify()
        raise AdbError("No device chosen.")

    print("\n  Several handsets attached:\n")
    for i, (ser, _, desc) in enumerate(usable, 1):
        print(f"    {i}. {desc}  [{ser}]")
    while True:
        answer = input("\n  Which one? (number, or q) ").strip().lower()
        if answer == "q":
            raise AdbError("No device chosen.")
        if answer.isdigit() and 1 <= int(answer) <= len(usable):
            return Device(adb_path, usable[int(answer) - 1][0]).identify()
        print("  Not a valid choice.")


def connect(serial=None, chooser=None):
    adb_path = find_adb()
    if not adb_path:
        raise AdbError("adb isn't installed or isn't on PATH.\n  "
                       + adb_install_hint().replace("\n", "\n  "))
    return pick_device(adb_path, serial, chooser)


# ============================================================ scoring

def _recent(stamp, days=RECENT_DAYS):
    try:
        when = datetime.strptime(stamp[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return datetime.now() - when <= timedelta(days=days)


def score_app(pkg, installer, ctx):
    score, why = 0, []
    flat = pkg.lower().replace("_", "").replace(".", "")

    known = ctx.get("known", {}).get(pkg)
    if known:
        score += KNOWN_BAD_SCORE
        seen = max(int(known.get("reports") or 1),
                   int(known.get("times_seen") or 0))
        why.append(f"IN BAD-APP DATABASE: {known['category']}"
                   + (f" - {known['label']}" if known.get("label") else "")
                   + f" (reported {seen}x, suggested: {known['action']})")
    if pkg in ctx["overlay"]:
        score += 3
        why.append("can draw over other apps")
    if pkg in ctx["accessibility"]:
        score += 4
        why.append("accessibility service enabled")
    if pkg in ctx["admins"]:
        score += 5
        why.append("registered as device admin")
    if pkg in ctx["listeners"]:
        score += 2
        why.append("can read all notifications")
    if pkg in ctx["battery"]:
        score += 1
        why.append("exempt from battery optimisation")

    launchers = ctx.get("launchers")
    if launchers is not None and pkg not in launchers:
        score += 2
        why.append("no home-screen icon (hidden app)")

    if installer is None:
        score += 3
        why.append("sideloaded (no installer recorded)")
    elif installer not in TRUSTED_INSTALLERS:
        score += 2
        why.append(f"installed by {installer}")

    details = ctx.get("details", {}).get(pkg, {})
    if _recent(details.get("first_install", "")):
        score += 1
        why.append(f"installed recently ({details['first_install'][:10]})")

    hits = [w for w in SUSPECT_WORDS if w in flat]
    if hits:
        score += 2
        why.append(f"junkware-typical name ({hits[0]})")

    return score, why


def gather(device, progress=None, db=None):
    """Full inventory. progress(text) is called as each stage finishes."""
    def step(msg):
        if progress:
            progress(msg)

    step("Reading package list")
    pkgs = device.third_party_packages()

    step(f"{len(pkgs)} user apps - reading permission state")
    ctx = {
        "accessibility": device.accessibility_packages(),
        "listeners": device.notification_listener_packages(),
        "battery": device.battery_exempt_packages(),
        "admins": device.device_admin_packages(),
    }

    step("Checking overlay permissions")
    ctx["overlay"] = device.overlay_packages(set(pkgs))

    step("Reading install dates and icons")
    ctx["details"] = device.package_details()
    ctx["launchers"] = device.launcher_packages()

    step("Comparing against the bad-app database")
    ctx["known"] = db.known(pkgs) if db else {}
    if db and ctx["known"]:
        db.mark_seen(ctx["known"])

    step("Scoring")
    rows = []
    for pkg, installer in pkgs.items():
        score, why = score_app(pkg, installer, ctx)
        name, colour = severity_of(score)
        details = ctx["details"].get(pkg, {})
        rows.append({
            "pkg": pkg,
            "installer": installer,
            "score": score,
            "why": why,
            "severity": name,
            "colour": colour,
            "protected": is_protected(pkg),
            "known": ctx["known"].get(pkg),
            "installed": details.get("first_install", ""),
            "version": details.get("version", ""),
        })
    rows.sort(key=lambda r: (-r["score"], r["pkg"]))
    step("Done")
    return rows, ctx


def remove_app(device, pkg, how, admins=None):
    """Apply one of uninstall / disable / declaw. Returns a result string."""
    if how == "declaw":
        return ", ".join(device.declaw(pkg))
    if how == "disable":
        return device.disable(pkg)
    return device.uninstall(pkg, (admins or {}).get(pkg))


# ============================================================ report

BROWSER_NOTE = """BROWSER PUSH NOTIFICATIONS
--------------------------
If ads still appear after the above, they are almost certainly web push
notifications rather than an installed app. This is the most common cause
and it looks identical to the customer.

  Chrome  ->  three dots  ->  Settings  ->  Site settings  ->  Notifications
              Revoke everything the customer doesn't recognise.
  Chrome  ->  Settings  ->  Privacy and security  ->  Clear browsing data
              (Cookies and site data, All time)
  Samsung Internet  ->  Settings  ->  Sites and downloads  ->  Notifications
"""


def build_report(device, rows, ctx, actions_log=None):
    flagged = [r for r in rows if r["score"] >= 3]
    out = []
    out.append("ANDROID POPUP / ADWARE TRIAGE")
    out.append("=" * 62)
    out.append(f"Date:     {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    out.append(f"Handset:  {device.model}")
    out.append(f"Android:  {device.release} (API {device.sdk})")
    out.append(f"Serial:   {device.serial}")
    out.append(f"Bench:    {platform.system()} {platform.release()}")
    out.append(f"User apps installed: {len(rows)}")
    known = [r for r in rows if r.get("known")]
    out.append(f"Known-bad apps found: {len(known)}")
    out.append("")
    out.append("FINDINGS")
    out.append("-" * 62)

    if not flagged:
        out.append("No installed app showed popup-capable permissions.")
        out.append("")
    for r in flagged:
        out.append(f"[{r['severity']}] {r['pkg']}  (score {r['score']})")
        for w in r["why"]:
            out.append(f"    - {w}")
        out.append("")

    admins = sorted(p for p in ctx["admins"] if not is_protected(p))
    if admins:
        out.append("DEVICE ADMIN APPS")
        out.append("-" * 62)
        out += [f"  {p}" for p in admins]
        out.append("")

    if actions_log:
        out.append("ACTIONS TAKEN")
        out.append("-" * 62)
        out += [f"  {line}" for line in actions_log]
        out.append("")

    out.append(BROWSER_NOTE)
    return "\n".join(out)


# ============================================================ CLI

def die(msg):
    print(f"\n  {msg}\n", file=sys.stderr)
    sys.exit(1)


def cli_progress(msg):
    print(f"  {msg}...")


def print_header(device):
    print()
    print("  " + paint(device.describe(), BLUE, bold=True))
    print()


def print_rows(rows):
    flagged = [r for r in rows if r["score"] >= 3]
    if not flagged:
        print("  Nothing scored above the noise floor.")
        print("  If ads are still appearing, they're most likely browser "
              "push notifications -\n  see the note at the bottom of "
              "`report`.\n")
        return flagged
    for r in flagged:
        tag = paint("  [system]", MUTED) if r["protected"] else ""
        badge = paint(f"{r['severity']:<7}", r["colour"], bold=True)
        print(f"  {badge} {r['score']:>2}  {r['pkg']}{tag}")
        for w in r["why"]:
            print(f"              {paint('·', MUTED)} {w}")
        print()
    quiet = len(rows) - len(flagged)
    if quiet:
        print(paint(f"  ({quiet} other user apps scored clean)\n", MUTED))
    return flagged


def open_db(args):
    try:
        return BadAppDB(getattr(args, "db", None))
    except sqlite3.Error as exc:
        die(f"could not open the database: {exc}")


def cmd_scan(args):
    device = connect(args.serial)
    print_header(device)
    rows, _ = gather(device, cli_progress, open_db(args))
    print()
    print_rows(rows)
    print("  Next: `watch` to catch the offender live, `clean` to remove "
          "known-bad apps,\n  or `fix` to start pulling teeth.\n")


def cmd_watch(args):
    device = connect(args.serial)
    print_header(device)
    print("  Watching the foreground. Leave the phone idle and wait for the "
          "ad to fire.")
    print(paint("  Ctrl-C to stop.\n", MUTED))

    baseline, seen = None, {}
    try:
        while True:
            pkg, act = device.foreground()
            if pkg and pkg != baseline:
                stamp = datetime.now().strftime("%H:%M:%S")
                first = pkg not in seen
                seen[pkg] = seen.get(pkg, 0) + 1
                marker = paint("<<<", SEVERE, bold=True) if (first and baseline) \
                    else "   "
                ad = paint("  [AD]", SEVERE, bold=True) \
                    if ad_like_activity(act) else ""
                print(f"  {paint(stamp, MUTED)}  {marker} {pkg}"
                      f"{'/' + act if act else ''}{ad}")
                baseline = pkg
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n\n  Foreground apps seen this session:\n")
        for pkg, count in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>3}x  {pkg}")
        print("\n  The one that appeared while you weren't touching the "
              "phone is your culprit.\n")


def cmd_fix(args):
    device = connect(args.serial)
    db = open_db(args)
    job = db.start_job(device)
    print_header(device)
    rows, ctx = gather(device, cli_progress, db)
    print()
    flagged = print_rows(rows)
    if not flagged:
        return

    print("  Per app:  [d]eclaw   [x] disable   [u] uninstall   [b]ad-list "
          "it   [s]kip   [q]uit\n")
    print(paint(
        "  Declaw strips overlay, notification and background-start rights "
        "but leaves the\n  app and its data installed - the safe default on "
        "a customer's handset.\n  Uninstall removes it for the current user "
        "only; a factory reset brings it back.\n", MUTED))

    log = []
    for r in flagged:
        pkg = r["pkg"]
        print(f"  {paint(r['severity'], r['colour'], bold=True)}  {pkg}")
        for w in r["why"]:
            print(f"      {paint('·', MUTED)} {w}")
        if r["protected"]:
            print(paint("      [system component - declaw only]", MUTED))

        while True:
            choice = input("      action> ").strip().lower()
            if choice in ("s", ""):
                break
            if choice == "q":
                print()
                return
            if choice == "b":
                if r["protected"]:
                    print("      Refusing - system component.")
                    continue
                db.add(pkg, category="adware", notes="added from `fix`")
                print(f"      {paint('→', BLUE)} added to bad-app database")
                continue
            if choice == "d":
                done = device.declaw(pkg)
                print(f"      {paint('→', BLUE)} {', '.join(done)}")
                log.append(f"{pkg}: declawed ({', '.join(done)})")
                db.log_action(job, pkg, "declaw", ", ".join(done))
                break
            if choice in ("x", "u") and r["protected"]:
                print("      Refusing - system component. Declaw instead.")
                continue
            if choice == "x":
                res = device.disable(pkg)
                print(f"      {paint('→', BLUE)} {res}")
                log.append(f"{pkg}: disabled")
                db.log_action(job, pkg, "disable", res)
                db.mark_removed(pkg)
                break
            if choice == "u":
                if input(f"      Really uninstall {pkg}? [y/N] ") \
                        .strip().lower() == "y":
                    res = device.uninstall(pkg, ctx["admins"].get(pkg))
                    print(f"      {paint('→', BLUE)} {res}")
                    log.append(f"{pkg}: uninstalled for user 0 ({res})")
                    db.log_action(job, pkg, "uninstall", res)
                    db.mark_removed(pkg)
                break
            print("      Unknown option. Use d / x / u / b / s / q.")
        print()

    if args.report:
        Path(args.report).write_text(
            build_report(device, rows, ctx, log) + "\n")
        print(f"  Report written to {args.report}")
    print("  Reboot the handset and idle-test it for a few minutes.\n")


def cmd_clean(args):
    device = connect(args.serial)
    db = open_db(args)
    print_header(device)
    rows, ctx = gather(device, cli_progress, db)
    targets = [r for r in rows if r["known"] and not r["protected"]]
    print()
    if not targets:
        print("  No app on this handset is in the bad-app database.\n")
        return
    print(f"  {len(targets)} known-bad app(s) on this handset:\n")
    for r in targets:
        action = f"{r['known']['action']:<9}"
        print(f"    {paint(action, r['colour'])}  {r['pkg']}"
              f"  {paint(r['known']['category'], MUTED)}")
    if not args.yes and input("\n  Apply the suggested action to all? "
                              "[y/N] ").strip().lower() != "y":
        print("  Aborted.\n")
        return
    job = db.start_job(device)
    print()
    for r in targets:
        how = r["known"]["action"]
        res = remove_app(device, r["pkg"], how, ctx["admins"])
        db.log_action(job, r["pkg"], how, res)
        db.mark_removed(r["pkg"])
        print(f"  {r['pkg']}\n      {paint('→', BLUE)} {how}: {res}")
    print("\n  Reboot and idle-test.\n")


def cmd_nuke(args):
    device = connect(args.serial)
    db = open_db(args)
    print_header(device)
    rows, ctx = gather(device, cli_progress, db)
    targets = [r for r in rows if r["score"] >= args.threshold]
    print()

    if not targets:
        print(f"  Nothing scored at or above {args.threshold}.\n")
        return

    print(f"  About to declaw {len(targets)} app(s). No uninstalls, nothing "
          "removed:\n")
    for r in targets:
        badge = paint("{:>2}".format(r["score"]), r["colour"])
        print(f"    {badge}  {r['pkg']}")
    if input("\n  Proceed? [y/N] ").strip().lower() != "y":
        print("  Aborted.\n")
        return

    print()
    job = db.start_job(device)
    log = []
    for r in targets:
        done = device.declaw(r["pkg"])
        print(f"  {r['pkg']}\n      {paint('→', BLUE)} {', '.join(done)}")
        log.append(f"{r['pkg']}: declawed ({', '.join(done)})")
        db.log_action(job, r["pkg"], "declaw", ", ".join(done))

    if args.report:
        Path(args.report).write_text(
            build_report(device, rows, ctx, log) + "\n")
        print(f"\n  Report written to {args.report}")
    print("\n  Reboot and idle-test.\n")


def cmd_report(args):
    device = connect(args.serial)
    rows, ctx = gather(device, cli_progress, open_db(args))
    text = build_report(device, rows, ctx)
    Path(args.outfile).write_text(text + "\n")
    print(f"\n  Written to {args.outfile}\n")
    print(text)


def cmd_db(args):
    db = open_db(args)
    if args.db_cmd == "list" or not args.db_cmd:
        apps = db.all(getattr(args, "search", "") or "")
        if not apps:
            print("\n  The bad-app database is empty.\n")
            return
        print()
        for a in apps:
            print(f"  {a['pkg']:<48} {a['category']:<22} {a['action']:<9} "
                  f"seen {a['times_seen']:>3}  {paint(a['source'], MUTED)}")
        s = db.stats()
        print(f"\n  {s['apps']} apps ({s['local']} added on this bench), "
              f"{s['jobs']} jobs logged.\n")
    elif args.db_cmd == "add":
        row = db.add(args.pkg, args.label or "", args.category, args.action,
                     args.notes or "")
        print(f"  added {row['pkg']} ({row['category']}, {row['action']})")
    elif args.db_cmd == "remove":
        print("  removed" if db.remove(args.pkg) else "  not in database")
    elif args.db_cmd == "export":
        n = db.export_json(args.path)
        print(f"  wrote {n} apps to {args.path or db.shared_file}")
    elif args.db_cmd == "import":
        added, updated = db.import_file(args.path)
        print(f"  {added} added, {updated} updated")
    elif args.db_cmd == "update":
        added, updated = db.update_from_url(args.url)
        print(f"  {added} added, {updated} updated from "
              f"{args.url or DEFAULT_DB_URL}")


# ============================================================ GUI

class App:
    """The bench window. Every adb call runs on a worker thread and reports
    back through `self.jobs`, so the window never freezes on a slow phone.

    `device` and the dialog hooks (`ask`, `inform`, `error`, `save_as`,
    `open_file`) are injectable so the test suite can drive every button
    against a fake handset."""

    POLL_MS = 100

    def __init__(self, root, db, serial=None, interval=0.6,
                 device=None, auto_connect=True):
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox

        self.tk, self.ttk = tk, ttk
        self.root, self.db = root, db
        self.serial, self.interval = serial, interval
        self.jobs = queue.Queue()
        self.device = None
        self.job_id = None
        self.rows, self.ctx, self.log_lines = [], None, []
        self.busy_count = 0
        self.watching = False
        self.watch_stop = threading.Event()
        self.watch_events = []
        self.culprit = None
        self.home_pkg = None
        self.event_seq = 0
        self.dialog = None

        self.ask = messagebox.askyesno
        self.inform = messagebox.showinfo
        self.error = messagebox.showerror
        self.save_as = filedialog.asksaveasfilename
        self.open_file = filedialog.askopenfilename

        self._build()
        self._drain_id = self.root.after(self.POLL_MS, self._drain)
        if device is not None:
            self.root.after(50, lambda: self._connected(device))
        elif auto_connect:
            self.root.after(300, self.do_connect)

    # ---------------------------------------------------------- styling

    def _style(self):
        tk, ttk = self.tk, self.ttk
        from tkinter import font as tkfont

        base = tkfont.nametofont("TkDefaultFont")
        family = "Segoe UI" if IS_WINDOWS else base.actual("family")
        base.configure(size=10)
        tkfont.nametofont("TkTextFont").configure(size=10)
        self.f_body = (family, 10)
        self.f_bold = (family, 10, "bold")
        self.f_small = (family, 9)
        self.f_title = (family, 15, "bold")
        self.f_big = (family, 13, "bold")

        self.root.configure(bg=NAVY_DEEP)
        s = ttk.Style(self.root)
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=NAVY_DEEP, foreground=TEXT,
                    fieldbackground=NAVY, borderwidth=0, font=self.f_body,
                    focuscolor=BLUE, troughcolor=NAVY, bordercolor=NAVY_LINE,
                    lightcolor=NAVY, darkcolor=NAVY)
        s.configure("TFrame", background=NAVY_DEEP)
        s.configure("Card.TFrame", background=NAVY)
        s.configure("Head.TFrame", background=NAVY)
        s.configure("TLabel", background=NAVY_DEEP, foreground=TEXT)
        s.configure("Card.TLabel", background=NAVY, foreground=TEXT)
        s.configure("CardMuted.TLabel", background=NAVY, foreground=MUTED,
                    font=self.f_small)
        s.configure("CardHead.TLabel", background=NAVY, foreground=BLUE,
                    font=self.f_big)
        s.configure("Brand.TLabel", background=NAVY, foreground=BLUE,
                    font=self.f_title)
        s.configure("Muted.TLabel", background=NAVY_DEEP, foreground=MUTED,
                    font=self.f_small)
        s.configure("Error.TLabel", background=NAVY_DEEP, foreground=SEVERE)
        s.configure("Section.TLabel", background=NAVY_DEEP, foreground=BLUE,
                    font=self.f_bold)

        for name, bg, fg, active in (
                ("TButton", BLUE, NAVY_DEEP, TEXT),
                ("Danger.TButton", DANGER, TEXT, SEVERE),
                ("Ghost.TButton", NAVY_PANEL, TEXT, NAVY_LINE),
                ("Good.TButton", GOOD, NAVY_DEEP, TEXT)):
            s.configure(name, background=bg, foreground=fg,
                        font=self.f_bold, padding=(12, 6), borderwidth=0,
                        relief="flat")
            s.map(name, background=[("disabled", NAVY_PANEL),
                                    ("active", active)],
                  foreground=[("disabled", NAVY_LINE)])

        s.configure("TCheckbutton", background=NAVY_DEEP, foreground=TEXT)
        s.map("TCheckbutton", background=[("active", NAVY_DEEP)],
              indicatorcolor=[("selected", BLUE), ("!selected", NAVY)])
        s.configure("TEntry", fieldbackground=NAVY, foreground=TEXT,
                    insertcolor=TEXT, padding=6)
        s.configure("TCombobox", fieldbackground=NAVY, foreground=TEXT,
                    background=NAVY_PANEL, arrowcolor=BLUE, padding=4)
        s.map("TCombobox", fieldbackground=[("readonly", NAVY)],
              foreground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", NAVY)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

        s.configure("TNotebook", background=NAVY_DEEP, borderwidth=0,
                    tabmargins=(16, 8, 16, 0), bordercolor=NAVY_DEEP,
                    lightcolor=NAVY_DEEP, darkcolor=NAVY_DEEP)
        s.configure("TNotebook.Tab", background=NAVY_DEEP, foreground=MUTED,
                    padding=(18, 8), font=self.f_bold, borderwidth=0,
                    bordercolor=NAVY_DEEP, lightcolor=NAVY_DEEP,
                    darkcolor=NAVY_DEEP)
        s.map("TNotebook.Tab",
              background=[("selected", NAVY_PANEL), ("active", NAVY)],
              foreground=[("selected", TEXT), ("active", TEXT)],
              lightcolor=[("selected", BLUE)],
              expand=[("selected", (0, 0, 0, 0))])

        s.configure("Treeview", background=NAVY, fieldbackground=NAVY,
                    foreground=TEXT, rowheight=28, borderwidth=0,
                    font=self.f_body, bordercolor=NAVY, lightcolor=NAVY,
                    darkcolor=NAVY)
        s.configure("Treeview.Heading", background=NAVY_PANEL,
                    foreground=BLUE, font=self.f_bold, borderwidth=0,
                    padding=(6, 5))
        s.map("Treeview.Heading", background=[("active", NAVY_LINE)])
        s.map("Treeview", background=[("selected", "#3F6E8C")])
        for sb in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            s.configure(sb, background=NAVY_PANEL, troughcolor=NAVY,
                        arrowcolor=BLUE, borderwidth=0, gripcount=0,
                        bordercolor=NAVY, lightcolor=NAVY_PANEL,
                        darkcolor=NAVY_PANEL)
            s.map(sb, background=[("active", NAVY_LINE)])
        s.configure("Sash", sashthickness=6, gripcount=0,
                    background=NAVY_DEEP)
        s.configure("TPanedwindow", background=NAVY_DEEP)

    # ---------------------------------------------------------- layout

    def _build(self):
        tk, ttk = self.tk, self.ttk
        self.root.title(f"adbust {__version__} - Android popup & adware "
                        "cleaner")
        self.root.geometry("1200x780")
        self.root.minsize(980, 640)
        self._style()

        # header -------------------------------------------------
        head = ttk.Frame(self.root, style="Head.TFrame", padding=(18, 12))
        head.pack(fill="x")
        brand = ttk.Frame(head, style="Head.TFrame")
        brand.pack(side="left")
        ttk.Label(brand, text="adbust", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(brand, text="popup & adware cleaner",
                  style="CardMuted.TLabel").pack(anchor="w")

        dev = ttk.Frame(head, style="Head.TFrame", padding=(28, 0))
        dev.pack(side="left", fill="x", expand=True)
        row = ttk.Frame(dev, style="Head.TFrame")
        row.pack(anchor="w")
        self.dot = tk.Canvas(row, width=14, height=14, bg=NAVY,
                             highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self.dot_id = self.dot.create_oval(2, 2, 12, 12, fill=DANGER,
                                           outline="")
        self.title = ttk.Label(row, text="No handset connected",
                               style="CardHead.TLabel")
        self.title.pack(side="left")
        self.subtitle = ttk.Label(dev, text="Plug in a phone with USB "
                                  "debugging on, then press Connect.",
                                  style="CardMuted.TLabel")
        self.subtitle.pack(anchor="w", pady=(2, 0))

        hb = ttk.Frame(head, style="Head.TFrame")
        hb.pack(side="right")
        self.b = {}
        self.b["install"] = ttk.Button(hb, text="Install APK…",
                                       style="Ghost.TButton",
                                       command=self.do_install)
        self.b["install"].pack(side="left", padx=(0, 8))
        self.b["connect"] = ttk.Button(hb, text="Connect",
                                       command=self.do_connect)
        self.b["connect"].pack(side="left")

        # status bar (packed before the tabs so it keeps its height)
        foot = ttk.Frame(self.root, style="Head.TFrame", padding=(18, 6))
        foot.pack(fill="x", side="bottom")
        self.status = ttk.Label(foot, text="Ready.", style="Card.TLabel")
        self.status.pack(side="left")
        self.spin = ttk.Label(foot, text="", style="CardMuted.TLabel")
        self.spin.pack(side="left", padx=10)
        self.dbstat = ttk.Label(foot, text="", style="CardMuted.TLabel")
        self.dbstat.pack(side="right")

        # tabs ---------------------------------------------------
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True)
        self.tab_apps = ttk.Frame(self.nb, padding=(16, 12))
        self.tab_watch = ttk.Frame(self.nb, padding=(16, 12))
        self.tab_db = ttk.Frame(self.nb, padding=(16, 12))
        self.tab_hist = ttk.Frame(self.nb, padding=(16, 12))
        self.nb.add(self.tab_apps, text="  Apps  ")
        self.nb.add(self.tab_watch, text="  Live popup watch  ")
        self.nb.add(self.tab_db, text="  Bad-app database  ")
        self.nb.add(self.tab_hist, text="  History  ")
        self.nb.bind("<<NotebookTabChanged>>", self._tab_changed)

        self._build_apps()
        self._build_watch()
        self._build_db()
        self._build_history()

        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh_db()
        self.refresh_buttons()

    def _tree(self, parent, cols, height=12, select="extended"):
        """Treeview + scrollbar in a card. cols = [(id, heading, width,
        anchor, stretch)]."""
        ttk = self.ttk
        wrap = ttk.Frame(parent, style="Card.TFrame")
        tree = ttk.Treeview(wrap, columns=[c[0] for c in cols],
                            show="headings", selectmode=select,
                            height=height)
        for cid, text, width, anchor, stretch in cols:
            tree.heading(cid, text=text,
                         command=lambda c=cid, t=tree: self._sort(t, c))
            tree.column(cid, width=width, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for name, colour in (("SEVERE", SEVERE), ("HIGH", HIGH),
                             ("MEDIUM", MEDIUM), ("LOW", BLUE),
                             ("clean", MUTED), ("home", MUTED),
                             ("good", GOOD)):
            tree.tag_configure(name, foreground=colour)
        tree.tag_configure("known", background="#4A2F3A")
        tree.tag_configure("gone", foreground=NAVY_LINE)
        return wrap, tree

    def _sort(self, tree, col):
        rows = [(tree.set(k, col), k) for k in tree.get_children("")]

        def key(v):
            try:
                return (0, float(v[0]))
            except ValueError:
                return (1, v[0].lower())
        desc = getattr(tree, "_sort_desc", {}).get(col, col == "score")
        rows.sort(key=key, reverse=desc)
        for i, (_, k) in enumerate(rows):
            tree.move(k, "", i)
        tree._sort_desc = {col: not desc}

    def _textbox(self, parent, height, fg=TEXT, font=None):
        t = self.tk.Text(parent, height=height, wrap="word", bg=NAVY, fg=fg,
                         relief="flat", padx=12, pady=10,
                         insertbackground=TEXT, highlightthickness=0,
                         font=font or self.f_body, borderwidth=0)
        t.tag_configure("h", foreground=BLUE, font=self.f_big)
        t.tag_configure("bad", foreground=SEVERE, font=self.f_bold)
        t.tag_configure("muted", foreground=MUTED)
        for name, colour in (("SEVERE", SEVERE), ("HIGH", HIGH),
                             ("MEDIUM", MEDIUM), ("LOW", BLUE),
                             ("clean", MUTED)):
            t.tag_configure(name, foreground=colour, font=self.f_bold)
        return t

    def _build_apps(self):
        tk, ttk = self.tk, self.ttk
        f = self.tab_apps
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 10))
        self.b["scan"] = ttk.Button(bar, text="Scan phone",
                                    command=self.do_scan)
        self.b["scan"].pack(side="left")
        self.b["clean"] = ttk.Button(bar, text="Remove known bad",
                                     style="Danger.TButton",
                                     command=self.do_clean)
        self.b["clean"].pack(side="left", padx=8)
        self.b["report"] = ttk.Button(bar, text="Save report…",
                                      style="Ghost.TButton",
                                      command=self.do_report)
        self.b["report"].pack(side="left")

        self.show_all = tk.BooleanVar(value=False)
        self.show_all_cb = ttk.Checkbutton(bar, text="Show clean apps",
                                           variable=self.show_all,
                                           command=self.fill_apps)
        self.show_all_cb.pack(side="right")
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.fill_apps())
        ttk.Entry(bar, textvariable=self.search, width=28) \
            .pack(side="right", padx=10)
        ttk.Label(bar, text="Filter", style="Muted.TLabel").pack(side="right")

        self.summary = ttk.Label(f, text="Connect a phone and press Scan.",
                                 style="Muted.TLabel")
        self.summary.pack(fill="x", pady=(0, 6))

        body = ttk.Panedwindow(f, orient="horizontal")
        body.pack(fill="both", expand=True)
        wrap, self.apps = self._tree(body, [
            ("sev", "Risk", 72, "center", False),
            ("score", "Score", 54, "center", False),
            ("pkg", "Package", 250, "w", True),
            ("db", "Database", 88, "center", False),
            ("installed", "Installed", 92, "center", False),
            ("source", "Source", 96, "w", False),
        ])
        body.add(wrap, weight=3)
        self.root.after(200, lambda: body.sashpos(0, max(
            660, body.winfo_width() - 470)))
        self.apps.bind("<<TreeviewSelect>>", lambda e: self.on_app_select())
        self.apps.bind("<Double-1>", lambda e: self.do_open_info())

        side = ttk.Frame(body, padding=(12, 0, 0, 0))
        body.add(side, weight=2)
        ttk.Label(side, text="Why it's flagged", style="Section.TLabel") \
            .pack(anchor="w")
        self.detail = self._textbox(side, 12)
        self.detail.pack(fill="both", expand=True, pady=(4, 10))

        acts = ttk.Frame(side)
        acts.pack(fill="x")
        for key, text, style, cmd, r, c in (
                ("uninstall", "Uninstall", "Danger.TButton",
                 self.do_uninstall, 0, 0),
                ("disable", "Disable", "TButton", self.do_disable, 0, 1),
                ("declaw", "Declaw", "TButton", self.do_declaw, 0, 2),
                ("markbad", "Add to bad list", "Ghost.TButton",
                 self.do_mark_bad, 1, 0),
                ("stop", "Force stop", "Ghost.TButton", self.do_force_stop,
                 1, 1),
                ("info", "App info on phone", "Ghost.TButton",
                 self.do_open_info, 1, 2)):
            self.b[key] = ttk.Button(acts, text=text, style=style,
                                     command=cmd)
            self.b[key].grid(row=r, column=c, sticky="ew", padx=(0, 6),
                             pady=(0, 6))
        for c in range(3):
            acts.columnconfigure(c, weight=1, uniform="acts")

        ttk.Label(side, text="Activity log", style="Section.TLabel") \
            .pack(anchor="w", pady=(8, 0))
        self.logbox = self._textbox(side, 7, fg=MUTED, font=self.f_small)
        self.logbox.pack(fill="both", expand=True, pady=(4, 0))

    def _build_watch(self):
        tk, ttk = self.tk, self.ttk
        f = self.tab_watch
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 10))
        self.b["watch"] = ttk.Button(bar, text="Start watching",
                                     command=self.toggle_watch)
        self.b["watch"].pack(side="left")
        self.b["wclear"] = ttk.Button(bar, text="Clear", style="Ghost.TButton",
                                      command=self.clear_watch)
        self.b["wclear"].pack(side="left", padx=8)
        ttk.Label(bar, text="Leave the phone idle on the home screen. "
                  "Anything that opens by itself shows up here.",
                  style="Muted.TLabel").pack(side="left", padx=10)

        # culprit banner
        card = tk.Frame(f, bg=NAVY, highlightthickness=2,
                        highlightbackground=NAVY_PANEL)
        card.pack(fill="x", pady=(0, 12))
        self.culprit_card = card
        inner = tk.Frame(card, bg=NAVY)
        inner.pack(fill="x", padx=16, pady=12)
        left = tk.Frame(inner, bg=NAVY)
        left.pack(side="left", fill="x", expand=True)
        self.culprit_head = tk.Label(left, text="Last app that popped up",
                                     bg=NAVY, fg=MUTED, font=self.f_small,
                                     anchor="w")
        self.culprit_head.pack(anchor="w")
        self.culprit_pkg = tk.Label(left, text="Nothing yet - start "
                                    "watching", bg=NAVY, fg=TEXT,
                                    font=self.f_title, anchor="w")
        self.culprit_pkg.pack(anchor="w")
        self.culprit_why = tk.Label(left, text="", bg=NAVY, fg=MUTED,
                                    font=self.f_body, anchor="w",
                                    justify="left")
        self.culprit_why.pack(anchor="w")
        cb = tk.Frame(inner, bg=NAVY)
        cb.pack(side="right")
        for key, text, style, how in (
                ("c_uninstall", "Uninstall", "Danger.TButton", "uninstall"),
                ("c_declaw", "Declaw", "TButton", "declaw"),
                ("c_markbad", "Add to bad list", "Ghost.TButton", "markbad")):
            self.b[key] = ttk.Button(cb, text=text, style=style,
                                     command=lambda h=how: self.act_culprit(h))
            self.b[key].pack(side="left", padx=(6, 0))

        ttk.Label(f, text="Everything that took the screen or drew over it",
                  style="Section.TLabel").pack(anchor="w")
        eb = ttk.Frame(f)
        eb.pack(fill="x", side="bottom", pady=(8, 0))
        ttk.Label(eb, text="Selected row:", style="Muted.TLabel") \
            .pack(side="left")
        for key, text, style, how in (
                ("e_uninstall", "Uninstall", "Danger.TButton", "uninstall"),
                ("e_declaw", "Declaw", "TButton", "declaw"),
                ("e_markbad", "Add to bad list", "Ghost.TButton", "markbad")):
            self.b[key] = ttk.Button(eb, text=text, style=style,
                                     command=lambda h=how: self.act_event(h))
            self.b[key].pack(side="left", padx=(8, 0))
        wrap, self.events = self._tree(f, [
            ("time", "Time", 90, "center", False),
            ("kind", "What", 90, "center", False),
            ("pkg", "Package", 330, "w", True),
            ("act", "Screen / window", 330, "w", True),
            ("db", "Database", 90, "center", False),
        ], select="browse")
        wrap.pack(fill="both", expand=True, pady=(4, 0))
        self.events.bind("<<TreeviewSelect>>",
                         lambda e: self.refresh_buttons())

    def _build_db(self):
        tk, ttk = self.tk, self.ttk
        f = self.tab_db
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 10))
        for key, text, style, cmd in (
                ("db_add", "Add…", "TButton", lambda: self.edit_entry(None)),
                ("db_edit", "Edit…", "Ghost.TButton", self.db_edit),
                ("db_del", "Delete", "Danger.TButton", self.db_delete),
                ("db_update", "Update from GitHub", "Good.TButton",
                 self.db_update),
                ("db_import", "Import…", "Ghost.TButton", self.db_import),
                ("db_export", "Export shared list…", "Ghost.TButton",
                 self.db_export)):
            self.b[key] = ttk.Button(bar, text=text, style=style, command=cmd)
            self.b[key].pack(side="left", padx=(0, 8))
        self.db_search = tk.StringVar()
        self.db_search.trace_add("write", lambda *_: self.refresh_db())
        ttk.Entry(bar, textvariable=self.db_search, width=26) \
            .pack(side="right")
        ttk.Label(bar, text="Search", style="Muted.TLabel") \
            .pack(side="right", padx=8)

        ttk.Label(
            f, style="Muted.TLabel",
            text="Every app here is flagged SEVERE the moment it is found "
                 "on a phone. 'Export shared list' writes badapps.json - "
                 "commit it so the next bench gets it.") \
            .pack(fill="x", pady=(0, 6))
        wrap, self.dbtree = self._tree(f, [
            ("pkg", "Package", 300, "w", True),
            ("label", "Name", 160, "w", True),
            ("category", "Category", 150, "w", False),
            ("action", "Fix", 80, "center", False),
            ("seen", "Seen", 60, "center", False),
            ("removed", "Removed", 80, "center", False),
            ("last", "Last seen", 95, "center", False),
            ("source", "Source", 100, "center", False),
        ])
        wrap.pack(fill="both", expand=True)
        self.dbtree.bind("<<TreeviewSelect>>",
                         lambda e: self.refresh_buttons())
        self.dbtree.bind("<Double-1>", lambda e: self.db_edit())

    def _build_history(self):
        ttk = self.ttk
        f = self.tab_hist
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=(0, 10))
        self.b["h_refresh"] = ttk.Button(bar, text="Refresh",
                                         style="Ghost.TButton",
                                         command=self.refresh_history)
        self.b["h_refresh"].pack(side="left")
        ttk.Label(bar, text="One row per phone connected. Select a job to "
                  "see what was done to it.", style="Muted.TLabel") \
            .pack(side="left", padx=12)
        wrap, self.jobtree = self._tree(f, [
            ("id", "#", 50, "center", False),
            ("started", "When", 150, "center", False),
            ("model", "Handset", 260, "w", True),
            ("android", "Android", 120, "center", False),
            ("serial", "Serial", 160, "w", False),
            ("n", "Actions", 70, "center", False),
        ], height=8, select="browse")
        wrap.pack(fill="both", expand=True)
        self.jobtree.bind("<<TreeviewSelect>>", lambda e: self.show_job())
        ttk.Label(f, text="Actions", style="Section.TLabel") \
            .pack(anchor="w", pady=(10, 4))
        wrap, self.acttree = self._tree(f, [
            ("ts", "Time", 150, "center", False),
            ("action", "Action", 90, "center", False),
            ("pkg", "Package", 320, "w", True),
            ("result", "Result", 360, "w", True),
        ], height=8, select="browse")
        wrap.pack(fill="both", expand=True)

    # ---------------------------------------------------------- plumbing

    def log(self, msg):
        self.logbox.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
        self.logbox.see("end")

    def set_status(self, msg):
        self.status.configure(text=msg)

    def run_bg(self, fn, done=None, label="Working"):
        """Run adb work off the UI thread; adb over USB is slow enough to
        freeze tkinter otherwise."""
        self.busy_count += 1
        self.spin.configure(text=f"● {label}…")
        self.refresh_buttons()

        def wrapper():
            try:
                self.jobs.put(("ok", fn(), done))
            except (AdbError, DBError) as exc:
                self.jobs.put(("err", str(exc), None))
            except Exception as exc:  # noqa: BLE001 - surface it, don't die
                self.jobs.put(("err", f"{type(exc).__name__}: {exc}", None))

        threading.Thread(target=wrapper, daemon=True).start()

    def note(self, msg):
        self.jobs.put(("note", msg, None))

    def _drain(self):
        try:
            while True:
                kind, payload, done = self.jobs.get_nowait()
                if kind == "err":
                    self._finish()
                    self.set_status("Error - see the message")
                    self.log("ERROR " + payload.replace("\n", " "))
                    if "Lost the handset" in payload:
                        self._disconnected()
                    self.error("adbust", payload)
                elif kind == "note":
                    self.set_status(payload)
                elif kind == "event":
                    self._add_event(payload)
                elif kind == "watch_err":
                    self._stop_watch_ui()
                    self.log("watch stopped: " + payload)
                    self.set_status("Watch stopped - " + payload)
                    if "Lost the handset" in payload:
                        self._disconnected()
                else:
                    self._finish()
                    if done:
                        done(payload)
        except queue.Empty:
            pass
        try:
            self._drain_id = self.root.after(self.POLL_MS, self._drain)
        except self.tk.TclError:
            pass  # window already closed

    def _finish(self):
        self.busy_count = max(0, self.busy_count - 1)
        if not self.busy_count:
            self.spin.configure(text="")
        self.refresh_buttons()

    @property
    def idle(self):
        return self.busy_count == 0

    def _enable(self, key, on):
        self.b[key].state(["!disabled"] if on else ["disabled"])

    def refresh_buttons(self):
        dev = self.device is not None
        idle = self.idle
        sel = self.selected_rows()
        removable = [r for r in sel if not r["protected"]]
        known_on_phone = self._known_targets()

        self._enable("connect", idle and not self.watching)
        self.b["connect"].configure(text="Reconnect" if dev else "Connect")
        self._enable("install", dev and idle)
        self._enable("scan", dev and idle)
        self._enable("clean", dev and idle and bool(known_on_phone))
        self.b["clean"].configure(
            text=f"Remove known bad ({len(known_on_phone)})"
            if known_on_phone else "Remove known bad")
        self._enable("report", bool(self.rows) and dev and idle)
        for key in ("declaw", "stop", "info"):
            self._enable(key, dev and idle and bool(sel))
        for key in ("uninstall", "disable"):
            self._enable(key, dev and idle and bool(removable))
        self._enable("markbad", bool(removable))
        self.b["markbad"].configure(
            text="Edit bad-list entry" if len(sel) == 1 and sel[0].get("known")
            else "Add to bad list")

        self._enable("watch", dev and (self.watching or idle))
        self.b["watch"].configure(text="Stop watching" if self.watching
                                  else "Start watching")
        self._enable("wclear", bool(self.watch_events) or
                     self.culprit is not None)
        cul = self.culprit is not None
        cul_ok = cul and not is_protected(self.culprit["pkg"])
        self._enable("c_uninstall", dev and idle and cul_ok)
        self._enable("c_declaw", dev and idle and cul)
        self._enable("c_markbad", cul_ok)
        ev = self.selected_event()
        ev_ok = ev is not None and not is_protected(ev["pkg"])
        self._enable("e_uninstall", dev and idle and ev_ok)
        self._enable("e_declaw", dev and idle and ev is not None)
        self._enable("e_markbad", ev_ok)

        dbsel = self.dbtree.selection()
        self._enable("db_edit", len(dbsel) == 1)
        self._enable("db_del", bool(dbsel))
        self._enable("db_update", idle)

    def _known_targets(self):
        return [r for r in self.rows if r.get("known")
                and not r["protected"] and not r.get("gone")]

    def _tab_changed(self, _e=None):
        tab = self.nb.index(self.nb.select())
        if tab == 2:
            self.refresh_db()
        elif tab == 3:
            self.refresh_history()

    # ---------------------------------------------------------- connect

    def do_connect(self):
        self.set_status("Looking for a handset…")
        self.run_bg(lambda: connect(self.serial), self._connected,
                    "Connecting")

    def _connected(self, device):
        self.device = device
        self.job_id = self.db.start_job(device)
        self.log_lines = []
        self.title.configure(text=device.model)
        self.subtitle.configure(text=f"Android {device.release} (API "
                                f"{device.sdk})   ·   serial {device.serial}"
                                f"   ·   job #{self.job_id}")
        self.dot.itemconfigure(self.dot_id, fill=GOOD)
        self.log(f"Connected to {device.model} [{device.serial}]")

        def home():
            try:
                return device.home_package()
            except AdbError:
                return None

        def got_home(pkg):
            self.home_pkg = pkg

        self.run_bg(home, got_home, "Reading launcher")
        self.do_scan()

    def _disconnected(self):
        self.device = None
        self.dot.itemconfigure(self.dot_id, fill=DANGER)
        self.title.configure(text="Handset disconnected")
        self.subtitle.configure(text="Replug it and press Connect.")
        self.refresh_buttons()

    def do_install(self):
        path = self.open_file(
            title="Install an APK on the phone",
            filetypes=[("Android package", "*.apk"), ("All files", "*.*")])
        if not path or self.device is None:
            return
        name = Path(path).name
        device = self.device

        def work():
            self.note(f"Installing {name}…")
            return device.install(path)

        def done(res):
            self.log(f"install {name}: {res}")
            self.db.log_action(self.job_id, name, "install", res)
            self.set_status(f"Install {name}: {res}")
            if "Success" in res:
                self.do_scan()

        self.run_bg(work, done, "Installing")

    # ---------------------------------------------------------- apps tab

    def do_scan(self):
        if self.device is None:
            return
        self.detail.delete("1.0", "end")
        device = self.device

        def done(result):
            self.rows, self.ctx = result
            self.fill_apps()
            flagged = [r for r in self.rows if r["score"] >= 3]
            known = self._known_targets()
            self.set_status(f"Scan complete - {len(flagged)} flagged, "
                            f"{len(known)} in the bad-app database.")
            self.log(f"Scan: {len(self.rows)} user apps, {len(flagged)} "
                     f"flagged, {len(known)} known bad")
            d = self.detail
            d.delete("1.0", "end")
            if known:
                d.insert("end", f"{len(known)} known-bad app(s) found\n\n",
                         "bad")
                d.insert("end", "Press 'Remove known bad' to apply the "
                         "database's suggested fix to all of them at once.")
            elif not flagged:
                d.insert("end", "Nothing flagged.\n\n", "h")
                d.insert("end", "Ads are most likely browser push "
                         "notifications rather than an installed app. Save "
                         "the report - the exact steps are at the bottom of "
                         "it. Or switch to Live popup watch and wait for one "
                         "to fire.")
            else:
                d.insert("end", "Select an app to see why it was "
                         "flagged.\n\n", "muted")
                d.insert("end", "Tip: start Live popup watch, leave the "
                         "phone idle, and the app that throws the ad names "
                         "itself.", "muted")
            self.refresh_db()

        self.run_bg(lambda: gather(device, self.note, self.db), done,
                    "Scanning")

    def fill_apps(self):
        tree = self.apps
        keep = set(tree.selection())
        tree.delete(*tree.get_children())
        needle = self.search.get().strip().lower()
        shown = 0
        for r in self.rows:
            if r["score"] < 3 and not self.show_all.get() and \
                    not r.get("gone"):
                continue
            if needle and needle not in r["pkg"].lower():
                continue
            tags = [r["severity"]]
            if r.get("known"):
                tags.append("known")
            if r.get("gone"):
                tags = ["gone"]
            src = r["installer"] or "sideloaded"
            if src == "com.android.vending":
                src = "Play Store"
            tree.insert("", "end", iid=r["pkg"], tags=tags, values=(
                r.get("gone") or r["severity"], r["score"],
                r["pkg"] + ("   [system]" if r["protected"] else ""),
                "KNOWN BAD" if r.get("known") else "",
                (r.get("installed") or "")[:10], src))
            shown += 1
        for k in keep:
            if tree.exists(k):
                tree.selection_add(k)
        flagged = sum(1 for r in self.rows if r["score"] >= 3)
        known = sum(1 for r in self.rows if r.get("known"))
        if self.rows:
            self.summary.configure(
                text=f"{len(self.rows)} user apps  ·  {flagged} flagged  ·  "
                     f"{known} in bad-app database  ·  showing {shown}")
        self.refresh_buttons()

    def selected_rows(self):
        picked = set(self.apps.selection())
        return [r for r in self.rows if r["pkg"] in picked
                and not r.get("gone")]

    def on_app_select(self):
        rows = self.selected_rows()
        d = self.detail
        d.delete("1.0", "end")
        if len(rows) == 1:
            r = rows[0]
            d.insert("end", r["pkg"] + "\n", "h")
            d.insert("end", r["severity"], r["severity"])
            d.insert("end", f"   score {r['score']}", "muted")
            if r.get("version"):
                d.insert("end", f"   ·   v{r['version']}", "muted")
            d.insert("end", "\n\n")
            for w in r["why"]:
                d.insert("end", "  •  " + w + "\n",
                         "bad" if w.startswith("IN BAD-APP") else ())
            if r.get("known") and r["known"].get("notes"):
                d.insert("end", f"\nNotes: {r['known']['notes']}\n", "muted")
            if r["protected"]:
                d.insert("end", "\nSystem component - declaw only.\n",
                         "muted")
        elif rows:
            d.insert("end", f"{len(rows)} apps selected\n\n", "h")
            for r in rows:
                d.insert("end", f"  {r['pkg']}\n")
        self.refresh_buttons()

    def _apply(self, pkgs, how, confirm=None):
        """Run uninstall / disable / declaw / stop over pkgs on the worker."""
        if not pkgs or self.device is None:
            return
        if confirm and not self.ask("adbust", confirm):
            return
        admins = (self.ctx or {}).get("admins", {})
        device = self.device

        def work():
            out = []
            for pkg in pkgs:
                self.note(f"{how.capitalize()}: {pkg}…")
                if how == "stop":
                    res = device.force_stop(pkg)
                else:
                    res = remove_app(device, pkg, how, admins)
                out.append((pkg, res))
            return out

        def done(results):
            for pkg, res in results:
                self.log(f"{pkg}: {how} -> {res}")
                self.log_lines.append(f"{pkg}: {how} ({res})")
                self.db.log_action(self.job_id, pkg, how, res)
                failed = "Failure" in res or "DELETE_FAILED" in res \
                    or "Exception" in res
                if how in ("uninstall", "disable") and not failed:
                    self.db.mark_removed(pkg)
                    for r in self.rows:
                        if r["pkg"] == pkg:
                            r["gone"] = "removed" if how == "uninstall" \
                                else "disabled"
                if self.culprit and self.culprit["pkg"] == pkg:
                    self.culprit_why.configure(
                        text=f"{how}: {res}", fg=SEVERE if failed else GOOD)
            self.fill_apps()
            self.refresh_db()
            self.set_status(f"{how.capitalize()} done on {len(results)} "
                            "app(s). Reboot and idle-test.")

        self.run_bg(work, done, how.capitalize())

    def do_uninstall(self):
        pkgs = [r["pkg"] for r in self.selected_rows() if not r["protected"]]
        self._apply(pkgs, "uninstall",
                    "Uninstall these for the current user?\n\n"
                    + "\n".join(pkgs))

    def do_disable(self):
        pkgs = [r["pkg"] for r in self.selected_rows() if not r["protected"]]
        self._apply(pkgs, "disable")

    def do_declaw(self):
        self._apply([r["pkg"] for r in self.selected_rows()], "declaw")

    def do_force_stop(self):
        self._apply([r["pkg"] for r in self.selected_rows()], "stop")

    def do_open_info(self):
        rows = self.selected_rows()
        if not rows or self.device is None:
            return
        pkg, device = rows[0]["pkg"], self.device
        self.run_bg(lambda: device.open_app_info(pkg),
                    lambda res: self.log(f"{pkg}: {res}"), "Opening")

    def do_mark_bad(self):
        rows = [r for r in self.selected_rows() if not r["protected"]]
        if len(rows) == 1:
            self.edit_entry(self.db.get(rows[0]["pkg"]) or {
                "pkg": rows[0]["pkg"],
                "notes": "; ".join(rows[0]["why"])})
            return
        for r in rows:
            if not self.db.get(r["pkg"]):
                self.db.add(r["pkg"], notes="; ".join(r["why"]))
        self.log(f"Added {len(rows)} app(s) to the bad-app database")
        self._rescore()

    def do_clean(self):
        targets = self._known_targets()
        if not targets:
            return
        lines = "\n".join(f"{r['known']['action']:>9}   {r['pkg']}"
                          for r in targets)
        if not self.ask("adbust", f"Apply the database's suggested fix to "
                        f"{len(targets)} known-bad app(s)?\n\n{lines}"):
            return
        groups = {}
        for r in targets:
            groups.setdefault(r["known"]["action"], []).append(r["pkg"])
        for how, pkgs in groups.items():
            self._apply(pkgs, how)

    def do_report(self):
        if not self.rows:
            return
        default = f"popup-triage-{datetime.now():%Y%m%d-%H%M}.txt"
        path = self.save_as(
            defaultextension=".txt", initialfile=default,
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        text = build_report(self.device, self.rows, self.ctx, self.log_lines)
        Path(path).write_text(text + "\n")
        self.log(f"Report saved to {path}")
        self.set_status(f"Report saved to {path}")

    def _rescore(self):
        """Re-apply database hits to the current scan without re-reading
        the phone."""
        if self.ctx is not None:
            self.ctx["known"] = self.db.known(r["pkg"] for r in self.rows)
            for r in self.rows:
                r["score"], r["why"] = score_app(r["pkg"], r["installer"],
                                                 self.ctx)
                r["severity"], r["colour"] = severity_of(r["score"])
                r["known"] = self.ctx["known"].get(r["pkg"])
            self.rows.sort(key=lambda r: (-r["score"], r["pkg"]))
            self.fill_apps()
            self.on_app_select()
        self.refresh_db()
        for ev in self.watch_events:
            ev["known"] = self.db.get(ev["pkg"])
            if self.events.exists(ev["iid"]):
                self.events.set(ev["iid"], "db",
                                "KNOWN BAD" if ev["known"] else "")
        if self.culprit:
            self.culprit["known"] = self.db.get(self.culprit["pkg"])
            self._show_culprit(self.culprit)

    # ---------------------------------------------------------- watch tab

    def toggle_watch(self):
        if self.watching:
            self.watch_stop.set()
            self._stop_watch_ui()
            self.log("--- watch stopped ---")
            self.set_status("Watch stopped.")
            return
        if self.device is None:
            return
        self.watch_stop = threading.Event()
        self.watching = True
        self.set_status("Watching. Leave the phone idle and wait for the "
                        "ad to fire.")
        self.log("--- watch started ---")
        self.culprit_card.configure(highlightbackground=BLUE)
        self.refresh_buttons()
        threading.Thread(target=self._watch_loop,
                         args=(self.device, self.watch_stop),
                         daemon=True).start()

    def _stop_watch_ui(self):
        self.watching = False
        self.culprit_card.configure(
            highlightbackground=SEVERE if self.culprit else NAVY_PANEL)
        self.refresh_buttons()

    def _watch_loop(self, device, stop):
        last_pkg, last_overlays, tick = None, set(), 0
        while not stop.is_set():
            full = tick % 3 == 0
            try:
                pkg, act, overlays = device.window_state(with_overlays=full)
            except AdbError as exc:
                self.jobs.put(("watch_err", str(exc), None))
                return
            if not full:
                overlays = last_overlays
            if pkg and pkg != last_pkg:
                self.jobs.put(("event", {"pkg": pkg, "act": act or "",
                                         "kind": "screen"}, None))
                last_pkg = pkg
            for o in sorted(overlays - last_overlays):
                self.jobs.put(("event", {"pkg": o, "act": "drawing over "
                                         "other apps", "kind": "overlay"},
                               None))
            last_overlays = overlays
            tick += 1
            stop.wait(self.interval)

    def _add_event(self, ev):
        pkg = ev["pkg"]
        kind = ev["kind"]
        home = pkg == self.home_pkg or pkg == "com.android.systemui"
        if kind == "screen":
            kind = "AD" if ad_like_activity(ev["act"]) else \
                ("home" if home else "app")
        elif kind == "overlay":
            kind = "OVERLAY"
        self.event_seq += 1
        ev.update(iid=f"e{self.event_seq}", kind=kind,
                  time=datetime.now().strftime("%H:%M:%S"),
                  known=self.db.get(pkg))
        self.watch_events.append(ev)
        tag = ("home" if kind == "home" else
               "SEVERE" if kind in ("AD", "OVERLAY") or ev["known"] else
               "MEDIUM")
        tags = [tag] + (["known"] if ev["known"] else [])
        self.events.insert("", 0, iid=ev["iid"], tags=tags, values=(
            ev["time"], kind, pkg, ev["act"],
            "KNOWN BAD" if ev["known"] else ""))
        for old in self.events.get_children()[500:]:
            self.events.delete(old)
        self.watch_events = self.watch_events[-500:]
        self.log(f"watch: {kind} {pkg} {ev['act']}")
        if kind != "home" and not (is_protected(pkg) and kind == "app"):
            self._show_culprit(ev)
        self.refresh_buttons()

    def _show_culprit(self, ev):
        self.culprit = ev
        self.culprit_pkg.configure(text=ev["pkg"])
        bits = [f"{ev['time']}  ·  {ev['kind']}  ·  {ev['act']}"]
        if ev.get("known"):
            bits.append(f"In the bad-app database: {ev['known']['category']}"
                        f" (suggested fix: {ev['known']['action']})")
        if is_protected(ev["pkg"]):
            bits.append("System component - declaw only")
        self.culprit_why.configure(text="\n".join(bits), fg=MUTED)
        hot = ev["kind"] in ("AD", "OVERLAY") or ev.get("known")
        self.culprit_head.configure(
            text="POPUP CAUGHT - this app just took the screen"
            if hot else "Last app that opened by itself (or that you opened)")
        self.culprit_pkg.configure(fg=SEVERE if hot else TEXT)
        self.culprit_card.configure(
            highlightbackground=SEVERE if hot else BLUE)

    def selected_event(self):
        sel = self.events.selection()
        if not sel:
            return None
        return next((e for e in self.watch_events if e["iid"] == sel[0]),
                    None)

    def _act_on(self, ev, how):
        if ev is None:
            return
        pkg = ev["pkg"]
        if how == "markbad":
            if is_protected(pkg):
                return
            self.edit_entry(self.db.get(pkg) or {
                "pkg": pkg, "category": "popup ads",
                "notes": f"caught popping up ({ev['kind']}: {ev['act']})"})
        elif how == "uninstall":
            if not is_protected(pkg):
                self._apply([pkg], "uninstall", f"Uninstall {pkg}?")
        else:
            self._apply([pkg], how)

    def act_culprit(self, how):
        self._act_on(self.culprit, how)

    def act_event(self, how):
        self._act_on(self.selected_event(), how)

    def clear_watch(self):
        self.events.delete(*self.events.get_children())
        self.watch_events = []
        self.culprit = None
        self.culprit_pkg.configure(text="Nothing yet", fg=TEXT)
        self.culprit_head.configure(text="Last app that popped up")
        self.culprit_why.configure(text="")
        self.culprit_card.configure(
            highlightbackground=BLUE if self.watching else NAVY_PANEL)
        self.refresh_buttons()

    # ---------------------------------------------------------- database tab

    def refresh_db(self):
        tree = self.dbtree
        keep = set(tree.selection())
        tree.delete(*tree.get_children())
        for a in self.db.all(self.db_search.get().strip()):
            tree.insert("", "end", iid=a["pkg"], tags=("SEVERE",), values=(
                a["pkg"], a["label"], a["category"], a["action"],
                a["times_seen"], a["times_removed"], a["last_seen"] or "",
                a["source"]))
        for k in keep:
            if tree.exists(k):
                tree.selection_add(k)
        s = self.db.stats()
        self.dbstat.configure(
            text=f"database: {s['apps']} bad apps ({s['local']} local)  ·  "
                 f"{s['jobs']} jobs  ·  {s['removed']} removals")
        self.refresh_buttons()

    def edit_entry(self, entry):
        """Add / edit dialog. entry None = blank add."""
        tk, ttk = self.tk, self.ttk
        entry = dict(entry or {})
        editing = bool(entry.get("pkg") and self.db.get(entry["pkg"]))
        win = tk.Toplevel(self.root)
        win.title("Bad-app entry")
        win.configure(bg=NAVY_DEEP)
        win.transient(self.root)
        win.resizable(False, False)
        self.dialog = win
        frm = ttk.Frame(win, padding=18)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Edit entry" if editing else
                  "Add to the bad-app database",
                  style="Section.TLabel").grid(row=0, column=0, columnspan=2,
                                               sticky="w", pady=(0, 10))
        vars_ = {}
        defaults = {"category": "adware", "action": "uninstall"}
        fields = (("pkg", "Package"), ("label", "App name (as shown)"),
                  ("category", "Category"), ("action", "Suggested fix"),
                  ("notes", "Notes"))
        for i, (key, label) in enumerate(fields, 1):
            ttk.Label(frm, text=label).grid(row=i, column=0, sticky="w",
                                            pady=4, padx=(0, 12))
            v = tk.StringVar(value=entry.get(key) or defaults.get(key, ""))
            vars_[key] = v
            if key in ("category", "action"):
                w = ttk.Combobox(frm, textvariable=v, state="readonly",
                                 values=CATEGORIES if key == "category"
                                 else ACTIONS, width=42)
            else:
                w = ttk.Entry(frm, textvariable=v, width=46)
            w.grid(row=i, column=1, sticky="ew", pady=4)
            if key == "pkg" and entry.get("pkg"):
                w.state(["readonly"])
        msg = ttk.Label(frm, text="", style="Error.TLabel")
        msg.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="w")

        def close():
            win.destroy()
            self.dialog = None

        def save():
            try:
                self.db.add(vars_["pkg"].get(), vars_["label"].get(),
                            vars_["category"].get(), vars_["action"].get(),
                            vars_["notes"].get())
            except DBError as exc:
                msg.configure(text=str(exc))
                self.dialog_error = str(exc)
                return
            self.log(f"Database: saved {vars_['pkg'].get().strip()}")
            close()
            self._rescore()

        bb = ttk.Frame(frm)
        bb.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e",
                pady=(12, 0))
        self.dialog_save = ttk.Button(bb, text="Save", command=save)
        self.dialog_save.pack(side="right")
        self.dialog_cancel = ttk.Button(bb, text="Cancel",
                                        style="Ghost.TButton", command=close)
        self.dialog_cancel.pack(side="right", padx=8)
        self.dialog_vars = vars_
        self.dialog_error = ""
        win.bind("<Return>", lambda e: save())
        win.bind("<Escape>", lambda e: close())
        win.protocol("WM_DELETE_WINDOW", close)
        return win

    def db_edit(self):
        sel = self.dbtree.selection()
        if len(sel) == 1:
            self.edit_entry(self.db.get(sel[0]))

    def db_delete(self):
        sel = list(self.dbtree.selection())
        if not sel or not self.ask(
                "adbust", "Remove these from the bad-app database?\n\n"
                + "\n".join(sel)):
            return
        for pkg in sel:
            self.db.remove(pkg)
        self.log(f"Database: removed {len(sel)} entr"
                 + ("y" if len(sel) == 1 else "ies"))
        self._rescore()

    def db_update(self):
        def done(res):
            added, updated = res
            self.log(f"Database update: {added} added, {updated} updated")
            self.set_status(f"Database updated - {added} new, {updated} "
                            "changed.")
            self._rescore()

        self.run_bg(self.db.update_from_url, done, "Downloading database")

    def db_import(self):
        path = self.open_file(title="Import a bad-app list",
                              filetypes=[("JSON", "*.json"),
                                         ("All files", "*.*")])
        if not path:
            return
        try:
            added, updated = self.db.import_file(path)
        except (DBError, OSError) as exc:
            self.error("adbust", str(exc))
            return
        self.log(f"Imported {path}: {added} added, {updated} updated")
        self._rescore()

    def db_export(self):
        path = self.save_as(defaultextension=".json",
                            initialfile="badapps.json",
                            initialdir=str(HERE),
                            filetypes=[("JSON", "*.json")])
        if not path:
            return
        n = self.db.export_json(path)
        self.log(f"Exported {n} apps to {path}")
        self.inform("adbust", f"Wrote {n} apps to\n{path}\n\nCommit and "
                    "push badapps.json so every bench gets them.")

    # ---------------------------------------------------------- history tab

    def refresh_history(self):
        tree = self.jobtree
        tree.delete(*tree.get_children())
        for j in self.db.jobs():
            tree.insert("", "end", iid=str(j["id"]), values=(
                j["id"], j["started"], j["model"], j["android"], j["serial"],
                j["n_actions"]))
        self.acttree.delete(*self.acttree.get_children())

    def show_job(self):
        sel = self.jobtree.selection()
        self.acttree.delete(*self.acttree.get_children())
        if not sel:
            return
        for a in self.db.actions_for(int(sel[0])):
            self.acttree.insert("", "end", iid=str(a["id"]), values=(
                a["ts"], a["action"], a["pkg"], a["result"]))

    # ---------------------------------------------------------- close

    def close(self):
        self.watch_stop.set()
        try:
            self.root.after_cancel(self._drain_id)
            self.root.destroy()
        except self.tk.TclError:
            pass


def cmd_gui(args):
    try:
        import tkinter as tk
    except ImportError:
        die("tkinter isn't available.\n"
            "  Fedora:  sudo dnf install python3-tkinter\n"
            "  Debian:  sudo apt install python3-tk\n"
            "  Windows: reinstall Python with the tcl/tk option ticked.\n"
            "  Meanwhile the CLI works fine: python adbust.py scan")
    root = tk.Tk()
    App(root, open_db(args), serial=args.serial,
        interval=getattr(args, "interval", 0.6))
    root.mainloop()


# ============================================================ entry

def main():
    p = argparse.ArgumentParser(
        prog="adbust",
        description="Android popup/adware triage over ADB.")
    p.add_argument("-s", "--serial", help="target a specific handset")
    p.add_argument("--db", help="path to the local database file")
    p.add_argument("--version", action="version",
                   version=f"adbust {__version__}")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("scan", help="score user apps for popup capability")

    w = sub.add_parser("watch", help="live-name the app that just popped up")
    w.add_argument("--interval", type=float, default=0.6,
                   help="seconds between polls (default 0.6)")

    f = sub.add_parser("fix", help="interactive per-app triage")
    f.add_argument("--report", help="also write a job-card report here")

    c = sub.add_parser("clean", help="remove every known-bad app found")
    c.add_argument("-y", "--yes", action="store_true",
                   help="don't ask for confirmation")

    n = sub.add_parser("nuke", help="bulk-declaw everything above a score")
    n.add_argument("--threshold", type=int, default=5,
                   help="minimum score to act on (default 5)")
    n.add_argument("--report", help="also write a job-card report here")

    r = sub.add_parser("report", help="write a summary for the job card")
    r.add_argument("outfile", nargs="?", default="popup-triage.txt")

    d = sub.add_parser("db", help="manage the bad-app database")
    dsub = d.add_subparsers(dest="db_cmd")
    dl = dsub.add_parser("list", help="show every known-bad app")
    dl.add_argument("search", nargs="?", default="")
    da = dsub.add_parser("add", help="add or update an entry")
    da.add_argument("pkg")
    da.add_argument("--label")
    da.add_argument("--category", default="adware", choices=CATEGORIES)
    da.add_argument("--action", default="uninstall", choices=ACTIONS)
    da.add_argument("--notes")
    dr = dsub.add_parser("remove", help="delete an entry")
    dr.add_argument("pkg")
    de = dsub.add_parser("export", help="write the shared badapps.json")
    de.add_argument("path", nargs="?")
    di = dsub.add_parser("import", help="merge a badapps.json file")
    di.add_argument("path")
    du = dsub.add_parser("update", help="download the newest shared list")
    du.add_argument("url", nargs="?")

    g = sub.add_parser("gui", help="point-and-click window")
    g.add_argument("--interval", type=float, default=0.6,
                   help="watch poll interval in seconds (default 0.6)")

    args = p.parse_args()
    if not args.cmd:
        # Double-clicked on Windows, or run bare - the window is the friendlier
        # default there.
        args = p.parse_args(sys.argv[1:] + ["gui"])

    handlers = {"scan": cmd_scan, "watch": cmd_watch, "fix": cmd_fix,
                "clean": cmd_clean, "nuke": cmd_nuke, "report": cmd_report,
                "db": cmd_db, "gui": cmd_gui}
    try:
        handlers[args.cmd](args)
    except (AdbError, DBError) as exc:
        die(str(exc))
    except KeyboardInterrupt:
        print("\n")


if __name__ == "__main__":
    main()
