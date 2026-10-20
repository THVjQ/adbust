"""A fake handset for tests. It subclasses the real Device and only swaps out
`_run`, so every parser and action in adbust.py runs for real against
canned adb output - and the fake keeps state, so an uninstall really makes
the app disappear from the next scan."""

import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import adbust  # noqa: E402

ADWARE = "com.fun.pdfscanner.free"
CLEANER = "com.super.junkcleaner"
GOOD = "com.whatsapp"
ADMIN = "com.lock.screen.admin"
ACCESS = "com.helper.accessibility"


class FakePhone(adbust.Device):
    def __init__(self):
        super().__init__("adb", "FAKE123")
        self.lock = threading.Lock()
        self.calls = []
        # pkg -> installer (None = sideloaded)
        self.packages = {
            ADWARE: None,
            CLEANER: "com.some.thirdpartystore",
            GOOD: "com.android.vending",
            ADMIN: None,
            ACCESS: "com.android.vending",
        }
        self.disabled = set()
        self.appops = {ADWARE: {"SYSTEM_ALERT_WINDOW": "allow"},
                       CLEANER: {"SYSTEM_ALERT_WINDOW": "allow"}}
        self.secure = {
            "enabled_accessibility_services":
                f"{ACCESS}/{ACCESS}.Svc:com.google.android.marvin.talkback/.T",
            "enabled_notification_listeners": f"{CLEANER}/.Listener",
        }
        self.launchers = {GOOD, CLEANER, ACCESS}  # ADWARE + ADMIN hide icons
        self.focus = ("com.sec.android.app.launcher",
                      "com.sec.android.app.launcher.activities.LauncherActivity")
        self.overlay_windows = set()
        self.started = []

    # -- the only override ---------------------------------------

    def _run(self, args, timeout=40):
        with self.lock:
            self.calls.append(tuple(args))
            return self._answer(list(args))

    def _answer(self, a):
        if a[0] == "install":
            return "Performing Streamed Install\nSuccess\n"
        assert a[0] == "shell", a
        cmd = a[1:]
        j = " ".join(cmd)
        if cmd[:1] == ["getprop"]:
            return {"ro.product.model": "SM-A546E",
                    "ro.product.brand": "samsung",
                    "ro.build.version.release": "14",
                    "ro.build.version.sdk": "34"}.get(cmd[1], "") + "\n"
        if j == "pm list packages -3 -i":
            return "".join(f"package:{p}  installer={i or 'null'}\n"
                           for p, i in self.packages.items())
        if cmd[:3] == ["pm", "list", "packages"]:
            return "".join(f"package:{p}\n" for p in self.packages
                           if cmd[3] in p)
        if cmd[:3] == ["settings", "get", "secure"]:
            return self.secure.get(cmd[3], "null") + "\n"
        if cmd[:3] == ["settings", "put", "secure"]:
            self.secure[cmd[3]] = cmd[4]
            return ""
        if cmd[:3] == ["settings", "delete", "secure"]:
            self.secure.pop(cmd[3], None)
            return "Deleted 1 rows\n"
        if j == "dumpsys deviceidle whitelist":
            return f"system,com.android.phone,1001\nuser,{CLEANER},10233\n"
        if cmd[:3] == ["dumpsys", "deviceidle", "whitelist"]:
            return "Removed: " + cmd[3][1:] + "\n"
        if j == "dumpsys device_policy":
            if ADMIN not in self.packages:
                return "Current Device Policy Manager state:\n"
            return ("Current Device Policy Manager state:\n  Enabled Device "
                    "Admins (User 0, provisioningState: 0):\n    "
                    f"{ADMIN}/.AdminReceiver:\n      uid=10240\n"
                    f"      ComponentInfo{{{ADMIN}/{ADMIN}.AdminReceiver}}\n")
        if j == "cmd appops query-op SYSTEM_ALERT_WINDOW allow":
            return "".join(f"{p}\n" for p, ops in self.appops.items()
                           if ops.get("SYSTEM_ALERT_WINDOW") == "allow"
                           and p in self.packages)
        if cmd[:3] == ["cmd", "appops", "set"]:
            self.appops.setdefault(cmd[3], {})[cmd[4]] = cmd[5]
            if cmd[4] == "SYSTEM_ALERT_WINDOW" and cmd[5] == "deny":
                self.overlay_windows.discard(cmd[3])
            return ""
        if j == "dumpsys package packages":
            out = []
            for p in self.packages:
                date = "2026-10-15 09:12:01" if p == ADWARE \
                    else "2025-01-04 11:00:00"
                out.append(f"  Package [{p}] (a1b2c3):\n    userId=10200\n"
                           f"    versionName=1.0.{len(p)}\n"
                           f"    timeStamp={date}\n"
                           f"    firstInstallTime={date}\n"
                           f"    lastUpdateTime={date}\n")
            return "Packages:\n" + "".join(out)
        if cmd[:3] == ["cmd", "package", "query-activities"]:
            return "".join(f"{p}/.Main\n" for p in self.launchers
                           if p in self.packages) + \
                "com.sec.android.app.launcher/.Launcher\n"
        if cmd[:3] == ["cmd", "package", "resolve-activity"]:
            return ("priority=0 preferredOrder=0 match=0x108000\n"
                    "com.sec.android.app.launcher/.activities.LauncherActivity\n")
        if cmd[:3] == ["dumpsys", "window", "windows"]:
            return self._windows()
        if cmd[:2] == ["dumpsys", "window"] or \
                cmd[:3] == ["dumpsys", "activity", "activities"]:
            return (f"  mCurrentFocus=Window{{77 u0 "
                    f"{self.focus[0]}/{self.focus[1]}}}\n")
        if cmd[:2] == ["am", "force-stop"]:
            return ""
        if cmd[:2] == ["am", "start"]:
            self.started.append(cmd[-1])
            return "Starting: Intent { act=... }\n"
        if cmd[:2] == ["pm", "disable-user"]:
            self.disabled.add(cmd[-1])
            return (f"Package {cmd[-1]} new state: disabled-user\n")
        if cmd[:2] == ["pm", "enable"]:
            self.disabled.discard(cmd[-1])
            return f"Package {cmd[-1]} new state: enabled\n"
        if cmd[:2] == ["dpm", "remove-active-admin"]:
            return ("java.lang.SecurityException: Attempt to remove non-test "
                    "admin\n")
        if cmd[:2] == ["pm", "uninstall"]:
            pkg = cmd[-1]
            if pkg == ADMIN:
                return "Failure [DELETE_FAILED_DEVICE_POLICY_MANAGER]\n"
            if self.packages.pop(pkg, "missing") == "missing":
                return "Failure [DELETE_FAILED_INTERNAL_ERROR]\n"
            return "Success\n"
        raise AssertionError(f"fake phone has no answer for: {j}")

    def _windows(self):
        out = ["WINDOW MANAGER WINDOWS (dumpsys window windows)"]
        n = 0
        for p in sorted(self.overlay_windows):
            out.append(f"  Window #{n} Window{{9{n} u0 {p}}}:\n"
                       f"    mDisplayId=0 rootTaskId=1\n"
                       f"    mOwnerUid=10234 showForAllUsers=false "
                       f"package={p} appop=SYSTEM_ALERT_WINDOW\n"
                       f"    mAttrs={{(0,0)(fillxfill) ty=APPLICATION_OVERLAY "
                       f"fmt=TRANSLUCENT}}\n    mHasSurface=true isVisible=true")
            n += 1
        out.append(f"  Window #{n} Window{{50 u0 com.android.systemui}}:\n"
                   "    package=com.android.systemui\n"
                   "    mAttrs={(0,0)(fillx89) ty=STATUS_BAR fmt=TRANSLUCENT}")
        out.append(f"  mCurrentFocus=Window{{77 u0 "
                   f"{self.focus[0]}/{self.focus[1]}}}")
        return "\n".join(out) + "\n"


def make():
    return FakePhone().identify()
