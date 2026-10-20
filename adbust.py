#!/usr/bin/env python3
"""
adbust - Android popup/adware triage for a repair bench.

Finds the app throwing full-screen ads and strips its ability to do it.
Runs on Windows and Linux. Needs adb; the handset needs USB debugging on.

    python adbust.py scan       score user apps for popup capability
    python adbust.py watch      live-name the app the moment an ad appears
    python adbust.py fix        interactive per-app triage
    python adbust.py nuke       bulk-declaw everything above a score
    python adbust.py report     write a summary for the job card
    python adbust.py gui        point-and-click window (same engine)

No third-party packages. The GUI uses tkinter from the standard library
(Fedora: sudo dnf install python3-tkinter).
"""

import argparse
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

IS_WINDOWS = os.name == "nt"
EXE = ".exe" if IS_WINDOWS else ""


# ============================================================ theme

# Kigga palette
BLUE = "#73AFCE"
NAVY = "#213D4F"
NAVY_DEEP = "#18303E"
NAVY_PANEL = "#2C4E63"
TEXT = "#F2F7FA"
MUTED = "#9DBACB"
SEVERE = "#FF9999"
HIGH = "#FFB366"
MEDIUM = "#FFD27F"


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
    if not sys.stdout.isatty():
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

TRUSTED_INSTALLERS = ("com.android.vending", "com.google.android.feedback")

SEVERITY = [
    (12, "SEVERE", SEVERE),
    (8, "HIGH", HIGH),
    (5, "MEDIUM", MEDIUM),
    (3, "LOW", BLUE),
    (0, "clean", MUTED),
]


def severity_of(score):
    for threshold, name, colour in SEVERITY:
        if score >= threshold:
            return name, colour
    return "clean", MUTED


def is_protected(pkg):
    return pkg.startswith(PROTECTED_PREFIXES)


# ============================================================ adb discovery

class AdbError(Exception):
    pass


def _candidate_adb_paths():
    here = Path(__file__).resolve().parent
    out = [here / f"adb{EXE}", here / "platform-tools" / f"adb{EXE}"]
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA", "")
        userp = os.environ.get("USERPROFILE", "")
        out += [
            Path(local) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
            Path(userp) / "Android" / "Sdk" / "platform-tools" / "adb.exe",
            Path(userp) / "scoop" / "apps" / "adb" / "current" / "adb.exe",
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


# ============================================================ device

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
                creationflags=(subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0),
            )
        except subprocess.TimeoutExpired:
            raise AdbError(f"adb timed out running: {' '.join(args)}")
        except FileNotFoundError:
            raise AdbError("adb vanished from its path mid-session")
        return proc.stdout or ""

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
            self.model = f"{brand} {self.model}"
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
        out = self.shell("dumpsys", "device_policy")
        return set(re.findall(r"([a-zA-Z][a-zA-Z0-9_.]{4,})/[a-zA-Z0-9_.$]+",
                              out))

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
            for line in out.splitlines():
                if not any(k in line for k in
                           ("mCurrentFocus", "mFocusedApp", "mResumedActivity",
                            "topResumedActivity")):
                    continue
                m = re.search(r"([a-zA-Z][a-zA-Z0-9_.]+)/([a-zA-Z0-9_.$]+)",
                              line)
                if m and m.group(1) not in ("null", "Window"):
                    self._focus_source = name
                    return m.group(1), m.group(2)
        return None, None

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

    def disable(self, pkg):
        return self.shell("pm", "disable-user", "--user", "0", pkg).strip() \
            or "disabled"

    def uninstall(self, pkg):
        return self.shell("pm", "uninstall", "--user", "0", pkg).strip() \
            or "removed"


def list_devices(adb_path):
    """[(serial, state, description)] for everything attached."""
    try:
        proc = subprocess.run(
            [adb_path, "devices", "-l"],
            capture_output=True, text=True, timeout=20, check=False,
            errors="replace",
            creationflags=(subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0),
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
    noperm = [d for d in devices if d[1] in ("no", "offline")
              or "permission" in d[1]]

    if serial:
        if serial not in [d[0] for d in usable]:
            raise AdbError(f"{serial} is not attached and ready.")
        return Device(adb_path, serial).identify()

    if not usable:
        if unauth:
            raise AdbError(
                "Handset is connected but unauthorised.\n"
                "  Tap 'Allow USB debugging' on the phone, tick 'always "
                "allow', then re-run.")
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

def score_app(pkg, installer, ctx):
    score, why = 0, []
    flat = pkg.lower().replace("_", "").replace(".", "")

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

    if installer is None:
        score += 3
        why.append("sideloaded (no installer recorded)")
    elif installer not in TRUSTED_INSTALLERS:
        score += 2
        why.append(f"installed by {installer}")

    hits = [w for w in SUSPECT_WORDS if w in flat]
    if hits:
        score += 2
        why.append(f"junkware-typical name ({hits[0]})")

    return score, why


def gather(device, progress=None):
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

    step("Scoring")
    rows = []
    for pkg, installer in pkgs.items():
        score, why = score_app(pkg, installer, ctx)
        name, colour = severity_of(score)
        rows.append({
            "pkg": pkg,
            "installer": installer,
            "score": score,
            "why": why,
            "severity": name,
            "colour": colour,
            "protected": is_protected(pkg),
        })
    rows.sort(key=lambda r: (-r["score"], r["pkg"]))
    step("Done")
    return rows, ctx


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


def cmd_scan(args):
    device = connect(args.serial)
    print_header(device)
    rows, _ = gather(device, cli_progress)
    print()
    print_rows(rows)
    print("  Next: `watch` to catch the offender live, or `fix` to start "
          "pulling teeth.\n")


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
                print(f"  {paint(stamp, MUTED)}  {marker} {pkg}"
                      f"{'/' + act if act else ''}")
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
    print_header(device)
    rows, ctx = gather(device, cli_progress)
    print()
    flagged = print_rows(rows)
    if not flagged:
        return

    print("  Per app:  [d]eclaw   [x] disable   [u] uninstall   [s]kip   "
          "[q]uit\n")
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
            if choice == "d":
                done = device.declaw(pkg)
                print(f"      {paint('→', BLUE)} {', '.join(done)}")
                log.append(f"{pkg}: declawed ({', '.join(done)})")
                break
            if choice in ("x", "u") and r["protected"]:
                print("      Refusing - system component. Declaw instead.")
                continue
            if choice == "x":
                print(f"      {paint('→', BLUE)} {device.disable(pkg)}")
                log.append(f"{pkg}: disabled")
                break
            if choice == "u":
                if input(f"      Really uninstall {pkg}? [y/N] ") \
                        .strip().lower() == "y":
                    print(f"      {paint('→', BLUE)} {device.uninstall(pkg)}")
                    log.append(f"{pkg}: uninstalled for user 0")
                break
            print("      Unknown option. Use d / x / u / s / q.")
        print()

    if args.report:
        Path(args.report).write_text(
            build_report(device, rows, ctx, log) + "\n")
        print(f"  Report written to {args.report}")
    print("  Reboot the handset and idle-test it for a few minutes.\n")


def cmd_nuke(args):
    device = connect(args.serial)
    print_header(device)
    rows, ctx = gather(device, cli_progress)
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
    log = []
    for r in targets:
        done = device.declaw(r["pkg"])
        print(f"  {r['pkg']}\n      {paint('→', BLUE)} {', '.join(done)}")
        log.append(f"{r['pkg']}: declawed ({', '.join(done)})")

    if args.report:
        Path(args.report).write_text(
            build_report(device, rows, ctx, log) + "\n")
        print(f"\n  Report written to {args.report}")
    print("\n  Reboot and idle-test.\n")


def cmd_report(args):
    device = connect(args.serial)
    rows, ctx = gather(device, cli_progress)
    text = build_report(device, rows, ctx)
    Path(args.outfile).write_text(text + "\n")
    print(f"\n  Written to {args.outfile}\n")
    print(text)


# ============================================================ GUI

def cmd_gui(args):
    try:
        import tkinter as tk
        from tkinter import ttk, filedialog, messagebox
    except ImportError:
        die("tkinter isn't available.\n"
            "  Fedora:  sudo dnf install python3-tkinter\n"
            "  Debian:  sudo apt install python3-tk\n"
            "  Windows: reinstall Python with the tcl/tk option ticked.\n"
            "  Meanwhile the CLI works fine: python adbust.py scan")

    jobs = queue.Queue()
    state = {"device": None, "rows": [], "ctx": None, "log": [],
             "watching": False}

    root = tk.Tk()
    root.title("adbust - Android popup triage")
    root.geometry("980x660")
    root.minsize(820, 560)
    root.configure(bg=NAVY_DEEP)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    style.configure(".", background=NAVY_DEEP, foreground=TEXT,
                    fieldbackground=NAVY, borderwidth=0)
    style.configure("TFrame", background=NAVY_DEEP)
    style.configure("Panel.TFrame", background=NAVY)
    style.configure("TLabel", background=NAVY_DEEP, foreground=TEXT)
    style.configure("Head.TLabel", background=NAVY_DEEP, foreground=BLUE,
                    font=("TkDefaultFont", 13, "bold"))
    style.configure("Muted.TLabel", background=NAVY_DEEP, foreground=MUTED)
    style.configure("TButton", background=BLUE, foreground=NAVY,
                    font=("TkDefaultFont", 10, "bold"), padding=(12, 7),
                    borderwidth=0)
    style.map("TButton",
              background=[("active", TEXT), ("disabled", NAVY_PANEL)],
              foreground=[("disabled", MUTED)])
    style.configure("Treeview", background=NAVY, fieldbackground=NAVY,
                    foreground=TEXT, rowheight=26, borderwidth=0)
    style.configure("Treeview.Heading", background=NAVY_PANEL,
                    foreground=BLUE, font=("TkDefaultFont", 10, "bold"),
                    borderwidth=0)
    style.map("Treeview", background=[("selected", BLUE)],
              foreground=[("selected", NAVY)])

    # -- layout ---------------------------------------------------

    top = ttk.Frame(root, padding=(16, 14, 16, 8))
    top.pack(fill="x")
    title = ttk.Label(top, text="No handset connected", style="Head.TLabel")
    title.pack(anchor="w")
    status = ttk.Label(top, text="Press Connect to begin.",
                       style="Muted.TLabel")
    status.pack(anchor="w", pady=(2, 0))

    bar = ttk.Frame(root, padding=(16, 4))
    bar.pack(fill="x")
    buttons = {}
    for key, label in (("connect", "Connect"), ("scan", "Scan"),
                       ("watch", "Watch"), ("declaw", "Declaw selected"),
                       ("disable", "Disable"), ("uninstall", "Uninstall"),
                       ("report", "Save report")):
        b = ttk.Button(bar, text=label)
        b.pack(side="left", padx=(0, 8))
        buttons[key] = b
    for key in ("scan", "watch", "declaw", "disable", "uninstall", "report"):
        buttons[key].state(["disabled"])

    body = ttk.Frame(root, padding=(16, 8))
    body.pack(fill="both", expand=True)
    body.columnconfigure(0, weight=3)
    body.columnconfigure(1, weight=2)
    body.rowconfigure(0, weight=1)

    tree_wrap = ttk.Frame(body, style="Panel.TFrame")
    tree_wrap.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
    tree = ttk.Treeview(tree_wrap, columns=("score", "pkg"),
                        show="headings", selectmode="extended")
    tree.heading("score", text="Score")
    tree.heading("pkg", text="Package")
    tree.column("score", width=70, anchor="center", stretch=False)
    tree.column("pkg", width=380, anchor="w")
    scroll = ttk.Scrollbar(tree_wrap, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    for name, colour in (("SEVERE", SEVERE), ("HIGH", HIGH),
                         ("MEDIUM", MEDIUM), ("LOW", BLUE),
                         ("clean", MUTED)):
        tree.tag_configure(name, foreground=colour)

    side = ttk.Frame(body)
    side.grid(row=0, column=1, sticky="nsew")
    side.rowconfigure(1, weight=1)
    side.rowconfigure(3, weight=1)
    side.columnconfigure(0, weight=1)

    ttk.Label(side, text="Why it's flagged").grid(row=0, column=0, sticky="w")
    detail = tk.Text(side, height=8, wrap="word", bg=NAVY, fg=TEXT,
                     relief="flat", padx=10, pady=8, insertbackground=TEXT,
                     highlightthickness=0)
    detail.grid(row=1, column=0, sticky="nsew", pady=(4, 12))

    ttk.Label(side, text="Activity log").grid(row=2, column=0, sticky="w")
    logbox = tk.Text(side, height=8, wrap="word", bg=NAVY, fg=MUTED,
                     relief="flat", padx=10, pady=8, insertbackground=TEXT,
                     highlightthickness=0)
    logbox.grid(row=3, column=0, sticky="nsew", pady=(4, 0))

    def log(msg):
        logbox.insert("end", f"{datetime.now():%H:%M:%S}  {msg}\n")
        logbox.see("end")

    def set_status(msg):
        status.configure(text=msg)

    def busy(flag):
        for key in ("connect", "scan", "declaw", "disable", "uninstall",
                    "report"):
            buttons[key].state(["disabled"] if flag else ["!disabled"])
        if not flag and state["device"]:
            buttons["scan"].state(["!disabled"])
            buttons["watch"].state(["!disabled"])
            buttons["report"].state(
                ["!disabled"] if state["rows"] else ["disabled"])
            for key in ("declaw", "disable", "uninstall"):
                buttons[key].state(
                    ["!disabled"] if tree.selection() else ["disabled"])

    # -- worker thread --------------------------------------------

    def run_bg(fn, done=None):
        """Run adb work off the UI thread; adb over USB is slow enough to
        freeze tkinter otherwise."""
        busy(True)

        def wrapper():
            try:
                jobs.put(("ok", fn(), done))
            except AdbError as exc:
                jobs.put(("err", str(exc), None))
            except Exception as exc:  # noqa: BLE001 - surface it, don't die
                jobs.put(("err", f"{type(exc).__name__}: {exc}", None))

        threading.Thread(target=wrapper, daemon=True).start()

    def drain():
        try:
            while True:
                kind, payload, done = jobs.get_nowait()
                if kind == "err":
                    busy(False)
                    set_status("Error")
                    log(payload.replace("\n", " "))
                    messagebox.showerror("adbust", payload)
                elif kind == "note":
                    set_status(payload)
                else:
                    busy(False)
                    if done:
                        done(payload)
        except queue.Empty:
            pass
        root.after(120, drain)

    def note(msg):
        jobs.put(("note", msg, None))

    # -- actions --------------------------------------------------

    def gui_chooser(usable):
        win = tk.Toplevel(root)
        win.title("Choose handset")
        win.configure(bg=NAVY_DEEP)
        win.transient(root)
        win.grab_set()
        ttk.Label(win, text="Several handsets attached:",
                  style="Head.TLabel").pack(padx=18, pady=(16, 8), anchor="w")
        chosen = tk.StringVar(value=usable[0][0])
        for ser, _, desc in usable:
            ttk.Radiobutton(win, text=f"{desc}   [{ser}]", value=ser,
                            variable=chosen).pack(padx=22, anchor="w")
        result = {}

        def ok():
            result["serial"] = chosen.get()
            win.destroy()

        ttk.Button(win, text="Use this one", command=ok).pack(pady=16)
        root.wait_window(win)
        return result.get("serial")

    def do_connect():
        def work():
            note("Looking for a handset...")
            return connect(args.serial, chooser=None)

        def done(device):
            state["device"] = device
            title.configure(text=device.describe())
            set_status("Connected. Press Scan.")
            log(f"Connected to {device.serial}")
            buttons["scan"].state(["!disabled"])
            buttons["watch"].state(["!disabled"])

        run_bg(work, done)

    def do_scan():
        tree.delete(*tree.get_children())
        detail.delete("1.0", "end")

        def work():
            return gather(state["device"], note)

        def done(result):
            rows, ctx = result
            state["rows"], state["ctx"] = rows, ctx
            flagged = [r for r in rows if r["score"] >= 3]
            for r in flagged:
                label = r["pkg"] + ("   [system]" if r["protected"] else "")
                tree.insert("", "end", iid=r["pkg"],
                            values=(r["score"], label),
                            tags=(r["severity"],))
            quiet = len(rows) - len(flagged)
            set_status(f"{len(flagged)} flagged, {quiet} clean, "
                       f"{len(rows)} user apps total.")
            log(f"Scan complete: {len(flagged)} flagged")
            buttons["report"].state(["!disabled"])
            if not flagged:
                detail.insert("1.0",
                              "Nothing flagged.\n\nAds are most likely "
                              "browser push notifications rather than an "
                              "installed app. Save the report - the exact "
                              "steps are at the bottom of it.")

        run_bg(work, done)

    def selected_rows():
        picked = set(tree.selection())
        return [r for r in state["rows"] if r["pkg"] in picked]

    def on_select(_event=None):
        rows = selected_rows()
        detail.delete("1.0", "end")
        for key in ("declaw", "disable", "uninstall"):
            buttons[key].state(["!disabled"] if rows else ["disabled"])
        if len(rows) == 1:
            r = rows[0]
            detail.insert("end", f"{r['pkg']}\n")
            detail.insert("end", f"{r['severity']}  (score {r['score']})\n\n")
            for w in r["why"]:
                detail.insert("end", f"  - {w}\n")
            if r["protected"]:
                detail.insert("end", "\nSystem component - declaw only.\n")
        elif rows:
            detail.insert("end", f"{len(rows)} apps selected.\n")

    tree.bind("<<TreeviewSelect>>", on_select)

    def do_declaw():
        rows = selected_rows()
        if not rows:
            return

        def work():
            out = []
            for r in rows:
                note(f"Declawing {r['pkg']}...")
                done = state["device"].declaw(r["pkg"])
                out.append((r["pkg"], ", ".join(done)))
            return out

        def done(results):
            for pkg, what in results:
                log(f"{pkg}: {what}")
                state["log"].append(f"{pkg}: declawed ({what})")
            set_status(f"Declawed {len(results)} app(s). Reboot and "
                       "idle-test.")

        run_bg(work, done)

    def do_disable():
        rows = [r for r in selected_rows() if not r["protected"]]
        if not rows:
            messagebox.showinfo("adbust", "Nothing selected that's safe to "
                                          "disable. Declaw instead.")
            return

        def work():
            out = []
            for r in rows:
                note(f"Disabling {r['pkg']}...")
                out.append((r["pkg"], state["device"].disable(r["pkg"])))
            return out

        def done(results):
            for pkg, what in results:
                log(f"{pkg}: {what}")
                state["log"].append(f"{pkg}: disabled")

        run_bg(work, done)

    def do_uninstall():
        rows = [r for r in selected_rows() if not r["protected"]]
        if not rows:
            messagebox.showinfo("adbust", "Nothing selected that's safe to "
                                          "remove. Declaw instead.")
            return
        names = "\n".join(r["pkg"] for r in rows)
        if not messagebox.askyesno(
                "adbust",
                f"Uninstall these for the current user?\n\n{names}\n\n"
                "A factory reset will bring them back."):
            return

        def work():
            out = []
            for r in rows:
                note(f"Uninstalling {r['pkg']}...")
                out.append((r["pkg"], state["device"].uninstall(r["pkg"])))
            return out

        def done(results):
            for pkg, what in results:
                log(f"{pkg}: {what}")
                state["log"].append(f"{pkg}: uninstalled for user 0")
                if tree.exists(pkg):
                    tree.delete(pkg)

        run_bg(work, done)

    def do_report():
        if not state["rows"]:
            return
        default = f"popup-triage-{datetime.now():%Y%m%d-%H%M}.txt"
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile=default,
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        text = build_report(state["device"], state["rows"], state["ctx"],
                            state["log"])
        Path(path).write_text(text + "\n")
        log(f"Report saved to {path}")
        set_status(f"Report saved to {path}")

    # -- watch mode -----------------------------------------------

    watch_stop = threading.Event()

    def watch_loop():
        baseline = None
        while not watch_stop.is_set():
            try:
                pkg, act = state["device"].foreground()
            except AdbError as exc:
                jobs.put(("err", str(exc), None))
                return
            if pkg and pkg != baseline:
                root.after(0, log, f"foreground -> {pkg}"
                                   + (f"/{act}" if act else ""))
                baseline = pkg
            watch_stop.wait(args.interval)

    def toggle_watch():
        if state["watching"]:
            watch_stop.set()
            state["watching"] = False
            buttons["watch"].configure(text="Watch")
            set_status("Watch stopped.")
            return
        watch_stop.clear()
        state["watching"] = True
        buttons["watch"].configure(text="Stop watching")
        set_status("Watching foreground. Leave the phone idle and wait for "
                   "the ad.")
        log("--- watch started ---")
        threading.Thread(target=watch_loop, daemon=True).start()

    buttons["connect"].configure(command=do_connect)
    buttons["scan"].configure(command=do_scan)
    buttons["watch"].configure(command=toggle_watch)
    buttons["declaw"].configure(command=do_declaw)
    buttons["disable"].configure(command=do_disable)
    buttons["uninstall"].configure(command=do_uninstall)
    buttons["report"].configure(command=do_report)

    def on_close():
        watch_stop.set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.after(120, drain)
    root.after(300, do_connect)
    root.mainloop()


# ============================================================ entry

def main():
    p = argparse.ArgumentParser(
        prog="adbust",
        description="Android popup/adware triage over ADB.")
    p.add_argument("-s", "--serial", help="target a specific handset")
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("scan", help="score user apps for popup capability")

    w = sub.add_parser("watch", help="live-name the app that just popped up")
    w.add_argument("--interval", type=float, default=0.6,
                   help="seconds between polls (default 0.6)")

    f = sub.add_parser("fix", help="interactive per-app triage")
    f.add_argument("--report", help="also write a job-card report here")

    n = sub.add_parser("nuke", help="bulk-declaw everything above a score")
    n.add_argument("--threshold", type=int, default=5,
                   help="minimum score to act on (default 5)")
    n.add_argument("--report", help="also write a job-card report here")

    r = sub.add_parser("report", help="write a summary for the job card")
    r.add_argument("outfile", nargs="?", default="popup-triage.txt")

    g = sub.add_parser("gui", help="point-and-click window")
    g.add_argument("--interval", type=float, default=0.6,
                   help="watch poll interval in seconds (default 0.6)")

    args = p.parse_args()
    if not args.cmd:
        # Double-clicked on Windows, or run bare - the window is the friendlier
        # default there.
        args = p.parse_args(["gui"])

    handlers = {"scan": cmd_scan, "watch": cmd_watch, "fix": cmd_fix,
                "nuke": cmd_nuke, "report": cmd_report, "gui": cmd_gui}
    try:
        handlers[args.cmd](args)
    except AdbError as exc:
        die(str(exc))
    except KeyboardInterrupt:
        print("\n")


if __name__ == "__main__":
    main()
