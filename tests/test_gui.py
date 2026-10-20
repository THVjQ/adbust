"""Drives every button in the window against the fake phone.

Buttons are pressed with ttk's invoke(), which does nothing on a disabled
button - so these tests also prove each button is enabled exactly when it
should be. Needs a display (any X server, or xvfb-run)."""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import adbust  # noqa: E402
import fakephone  # noqa: E402
from fakephone import ADWARE, CLEANER, GOOD, ADMIN  # noqa: E402

try:
    import tkinter as tk
    tk.Tk().destroy()
    HAVE_DISPLAY = True
except Exception:  # noqa: BLE001
    HAVE_DISPLAY = False


@unittest.skipUnless(HAVE_DISPLAY, "needs a display")
class GuiButtons(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="adbust-gui-")
        self.db = adbust.BadAppDB(os.path.join(self.dir, "t.db"),
                                  os.path.join(self.dir, "badapps.json"))
        self.phone = fakephone.make()
        self.root = tk.Tk()
        self.app = adbust.App(self.root, self.db, device=self.phone,
                              interval=0.05)
        self.asked = []
        self.errors = []
        self.app.ask = lambda t, m: self.asked.append(m) or True
        self.app.error = lambda t, m: self.errors.append(m)
        self.app.inform = lambda t, m: None
        self.pump()

    def tearDown(self):
        self.app.close()
        self.db.close()

    def pump(self, until=None, timeout=6):
        end = time.time() + timeout
        quiet = 0
        while time.time() < end:
            self.root.update()
            done = until() if until else (self.app.idle and
                                          self.app.jobs.empty())
            quiet = quiet + 1 if done else 0
            if quiet > 5:
                return
            time.sleep(0.01)
        self.fail("timed out waiting for the window")

    def press(self, key):
        btn = self.app.b[key]
        self.assertTrue(btn.instate(["!disabled"]),
                        f"button {key!r} is disabled")
        btn.invoke()
        self.pump()

    def enabled(self, key):
        return self.app.b[key].instate(["!disabled"])

    def select(self, *pkgs):
        self.app.apps.selection_set(pkgs)
        self.app.on_app_select()

    # ------------------------------------------------------------------

    def test_connect_scans_and_shows_flagged(self):
        shown = self.app.apps.get_children()
        self.assertIn(ADWARE, shown)
        self.assertNotIn(GOOD, shown)            # clean apps hidden
        self.assertEqual(self.app.title.cget("text"), "Samsung SM-A546E")
        self.assertEqual(self.app.home_pkg, "com.sec.android.app.launcher")
        self.assertEqual(self.app.b["connect"].cget("text"), "Reconnect")
        self.assertFalse(self.enabled("uninstall"))  # nothing selected
        self.assertFalse(self.enabled("clean"))      # nothing known yet

    def test_show_clean_and_filter(self):
        self.app.show_all_cb.invoke()
        self.pump()
        self.assertIn(GOOD, self.app.apps.get_children())
        self.app.search.set("junk")
        self.pump()
        self.assertEqual(self.app.apps.get_children(), (CLEANER,))

    def test_rescan(self):
        n = len(self.phone.calls)
        self.press("scan")
        self.assertGreater(len(self.phone.calls), n)

    def test_uninstall_selected(self):
        self.select(ADWARE)
        self.assertIn(ADWARE, self.app.detail.get("1.0", "end"))
        self.press("uninstall")
        self.assertTrue(self.asked)
        self.assertNotIn(ADWARE, self.phone.packages)
        self.assertEqual(self.app.apps.set(ADWARE, "sev"), "removed")
        self.assertFalse(self.enabled("uninstall"))  # gone rows unselectable
        hist = self.db.actions_for(self.app.job_id)
        self.assertEqual((hist[-1]["action"], hist[-1]["result"]),
                         ("uninstall", "Success"))

    def test_uninstall_device_admin_opens_app_info(self):
        self.select(ADMIN)
        self.press("uninstall")
        self.assertIn(ADMIN, self.phone.packages)
        self.assertIn(f"package:{ADMIN}", self.phone.started)
        self.assertNotEqual(self.app.apps.set(ADMIN, "sev"), "removed")

    def test_disable_declaw_stop_info(self):
        self.select(CLEANER)
        self.press("disable")
        self.assertIn(CLEANER, self.phone.disabled)
        self.select(ADWARE)
        self.press("declaw")
        self.assertEqual(self.phone.appops[ADWARE]["SYSTEM_ALERT_WINDOW"],
                         "deny")
        self.press("stop")
        self.assertIn(("shell", "am", "force-stop", ADWARE), self.phone.calls)
        self.press("info")
        self.assertIn(f"package:{ADWARE}", self.phone.started)

    def test_protected_rows_are_declaw_only(self):
        self.phone.packages["com.samsung.android.bad"] = None
        self.press("scan")
        self.select("com.samsung.android.bad")
        self.assertFalse(self.enabled("uninstall"))
        self.assertFalse(self.enabled("disable"))
        self.assertFalse(self.enabled("markbad"))
        self.assertTrue(self.enabled("declaw"))

    def test_mark_bad_dialog_then_remove_known_bad(self):
        self.select(ADWARE)
        self.press("markbad")
        dlg = self.app.dialog
        self.assertIsNotNone(dlg)
        self.app.dialog_vars["label"].set("PDF Scanner Free")
        self.app.dialog_save.invoke()
        self.pump()
        self.assertIsNone(self.app.dialog)
        self.assertEqual(self.db.get(ADWARE)["label"], "PDF Scanner Free")
        # row is now SEVERE, and the one-click clean button lights up
        self.assertEqual(self.app.apps.set(ADWARE, "sev"), "SEVERE")
        self.assertEqual(self.app.apps.set(ADWARE, "db"), "KNOWN BAD")
        self.assertEqual(self.app.b["clean"].cget("text"),
                         "Remove known bad (1)")
        self.press("clean")
        self.assertNotIn(ADWARE, self.phone.packages)
        self.assertEqual(self.db.get(ADWARE)["times_removed"], 1)
        self.assertFalse(self.enabled("clean"))

    def test_mark_bad_multi_select(self):
        self.select(ADWARE, CLEANER)
        self.press("markbad")
        self.assertIsNotNone(self.db.get(ADWARE))
        self.assertIsNotNone(self.db.get(CLEANER))

    def test_clean_uses_each_entrys_action(self):
        self.db.add(ADWARE, action="uninstall")
        self.db.add(CLEANER, action="disable")
        self.press("scan")
        self.press("clean")
        self.assertNotIn(ADWARE, self.phone.packages)
        self.assertIn(CLEANER, self.phone.disabled)

    def test_report(self):
        out = os.path.join(self.dir, "r.txt")
        self.app.save_as = lambda **k: out
        self.press("report")
        self.assertIn("ANDROID POPUP", open(out).read())
        self.app.save_as = lambda **k: ""   # cancelled dialog is harmless
        self.press("report")

    def test_install_apk(self):
        apk = os.path.join(self.dir, "x.apk")
        open(apk, "wb").close()
        self.app.open_file = lambda **k: apk
        self.press("install")
        self.assertIn(("install", "-r", apk), self.phone.calls)

    # ---- live watch ---------------------------------------------------

    def test_watch_catches_ad_and_overlay_and_removes(self):
        self.app.nb.select(1)
        self.press("watch")
        self.assertTrue(self.app.watching)
        self.assertEqual(self.app.b["watch"].cget("text"), "Stop watching")
        self.assertFalse(self.enabled("connect"))
        self.pump(lambda: len(self.app.watch_events) >= 1)
        self.assertEqual(self.app.watch_events[0]["kind"], "home")
        self.assertIsNone(self.app.culprit)  # the launcher is not a culprit

        self.phone.focus = (ADWARE, "com.google.android.gms.ads.AdActivity")
        self.pump(lambda: self.app.culprit is not None)
        self.assertEqual(self.app.culprit["pkg"], ADWARE)
        self.assertEqual(self.app.culprit["kind"], "AD")
        self.assertIn("POPUP CAUGHT", self.app.culprit_head.cget("text"))

        self.phone.overlay_windows = {CLEANER}
        self.pump(lambda: self.app.culprit["pkg"] == CLEANER)
        self.assertEqual(self.app.culprit["kind"], "OVERLAY")

        # banner buttons act on the culprit
        self.press("c_declaw")
        self.assertEqual(self.phone.appops[CLEANER]["SYSTEM_ALERT_WINDOW"],
                         "deny")
        self.press("c_markbad")
        self.app.dialog_save.invoke()
        self.pump()
        self.assertEqual(self.db.get(CLEANER)["category"], "popup ads")

        # row buttons act on the selected event
        ad_row = next(e for e in self.app.watch_events if e["kind"] == "AD")
        self.app.events.selection_set(ad_row["iid"])
        self.pump()
        self.press("e_uninstall")
        self.assertNotIn(ADWARE, self.phone.packages)
        self.press("e_declaw")
        self.press("e_markbad")
        self.app.dialog_cancel.invoke()
        self.pump()
        self.assertIsNone(self.db.get(ADWARE))

        self.press("watch")
        self.assertFalse(self.app.watching)
        self.press("wclear")
        self.assertEqual(self.app.events.get_children(), ())
        self.assertIsNone(self.app.culprit)
        self.assertFalse(self.enabled("c_uninstall"))

    def test_culprit_uninstall(self):
        self.press("watch")
        self.phone.focus = (ADWARE, f"{ADWARE}.AdActivity")
        self.pump(lambda: self.app.culprit is not None)
        self.press("c_uninstall")
        self.assertNotIn(ADWARE, self.phone.packages)
        self.press("watch")

    def test_watch_survives_lost_phone(self):
        self.press("watch")

        def boom(*a, **k):
            raise adbust.AdbError("Lost the handset: device offline")
        self.phone.window_state = boom
        self.pump(lambda: not self.app.watching)
        self.assertIsNone(self.app.device)
        self.assertFalse(self.enabled("scan"))
        self.assertTrue(self.enabled("connect"))

    # ---- database tab -------------------------------------------------

    def test_database_tab_buttons(self):
        self.app.nb.select(2)
        self.press("db_add")
        v = self.app.dialog_vars
        v["pkg"].set("not valid")
        self.app.dialog_save.invoke()
        self.pump()
        self.assertIn("not a valid package", self.app.dialog_error)
        v["pkg"].set("com.evil.wallpaper")
        v["category"].set("malware")
        v["action"].set("disable")
        self.app.dialog_save.invoke()
        self.pump()
        self.assertEqual(self.db.get("com.evil.wallpaper")["action"],
                         "disable")
        self.assertIn("com.evil.wallpaper", self.app.dbtree.get_children())

        self.app.dbtree.selection_set("com.evil.wallpaper")
        self.pump()
        self.press("db_edit")
        self.app.dialog_vars["notes"].set("edited")
        self.app.dialog_save.invoke()
        self.pump()
        self.assertEqual(self.db.get("com.evil.wallpaper")["notes"], "edited")

        out = os.path.join(self.dir, "export.json")
        self.app.save_as = lambda **k: out
        self.press("db_export")
        self.assertIn("com.evil.wallpaper", open(out).read())

        self.app.dbtree.selection_set("com.evil.wallpaper")
        self.pump()
        self.press("db_del")
        self.assertIsNone(self.db.get("com.evil.wallpaper"))

        self.app.open_file = lambda **k: out
        self.press("db_import")
        self.assertIsNotNone(self.db.get("com.evil.wallpaper"))

        bad = os.path.join(self.dir, "bad.json")
        open(bad, "w").write("{oops")
        self.app.open_file = lambda **k: bad
        self.press("db_import")
        self.assertTrue(any("JSON" in e for e in self.errors))

        self.app.db_search.set("evil")
        self.pump()
        self.assertEqual(self.app.dbtree.get_children(),
                         ("com.evil.wallpaper",))

    def test_database_update_button(self):
        src = os.path.join(self.dir, "remote.json")
        open(src, "w").write('{"apps":[{"pkg":"%s"}]}' % ADWARE)
        adbust.DEFAULT_DB_URL = "file://" + src
        try:
            self.press("db_update")
        finally:
            adbust.DEFAULT_DB_URL = "https://raw.githubusercontent.com/" \
                "THVjQ/adbust/main/badapps.json"
        self.assertIsNotNone(self.db.get(ADWARE))
        # the scan on screen re-scores without touching the phone
        self.assertEqual(self.app.apps.set(ADWARE, "db"), "KNOWN BAD")

    # ---- history tab --------------------------------------------------

    def test_history_tab(self):
        self.select(ADWARE)
        self.press("declaw")
        self.app.nb.select(3)
        self.pump()
        self.press("h_refresh")
        jobs = self.app.jobtree.get_children()
        self.assertEqual(jobs[0], str(self.app.job_id))
        self.app.jobtree.selection_set(jobs[0])
        self.pump()
        acts = self.app.acttree.get_children()
        self.assertEqual(self.app.acttree.set(acts[0], "action"), "declaw")

    def test_reconnect_button_errors_cleanly_without_phone(self):
        adbust_connect = adbust.connect

        def no_phone(*a, **k):
            raise adbust.AdbError("No device detected.")
        adbust.connect = no_phone
        try:
            self.press("connect")
        finally:
            adbust.connect = adbust_connect
        self.assertEqual(self.errors, ["No device detected."])
        self.assertTrue(self.enabled("connect"))

    def test_every_button_is_exercised(self):
        """Guard: a new button added to the window must get a test."""
        src = open(__file__).read()
        untested = [k for k in self.app.b
                    if f'"{k}"' not in src and f"'{k}'" not in src]
        self.assertEqual(untested, [])


if __name__ == "__main__":
    unittest.main()
