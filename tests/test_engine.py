import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import adbust  # noqa: E402
import fakephone  # noqa: E402
from fakephone import ADWARE, CLEANER, GOOD, ADMIN, ACCESS  # noqa: E402


def temp_db(shared=None):
    d = tempfile.mkdtemp(prefix="adbust-test-")
    shared_path = os.path.join(d, "badapps.json")
    if shared is not None:
        with open(shared_path, "w") as f:
            json.dump({"schema": 1, "apps": shared}, f)
    return adbust.BadAppDB(os.path.join(d, "t.db"), shared_path), d


class Parsers(unittest.TestCase):
    def test_focus(self):
        text = ("  mCurrentFocus=Window{1a u0 com.foo.bar/com.foo.bar."
                "AdActivity}\n")
        self.assertEqual(adbust._parse_focus(text),
                         ("com.foo.bar", "com.foo.bar.AdActivity"))
        self.assertEqual(adbust._parse_focus("mCurrentFocus=null"),
                         (None, None))

    def test_overlays_only_visible_overlay_types(self):
        text = (
            "  Window #0 Window{1 u0 com.bad.app}:\n    package=com.bad.app\n"
            "    mAttrs={ty=APPLICATION_OVERLAY}\n    isVisible=true\n"
            "  Window #1 Window{2 u0 com.hidden.app}:\n"
            "    package=com.hidden.app\n    mAttrs={ty=APPLICATION_OVERLAY}\n"
            "    isVisible=false\n"
            "  Window #2 Window{3 u0 com.normal.app}:\n"
            "    package=com.normal.app\n    mAttrs={ty=BASE_APPLICATION}\n")
        self.assertEqual(adbust.parse_overlays(text), {"com.bad.app"})

    def test_package_dump(self):
        text = ("Packages:\n  Package [com.a.b] (1):\n    versionName=2.1\n"
                "    firstInstallTime=2026-10-01 10:00:00\n"
                "  Package [com.c.d] (2):\n    versionName=1\n")
        info = adbust.parse_package_dump(text)
        self.assertEqual(info["com.a.b"]["version"], "2.1")
        self.assertEqual(info["com.a.b"]["first_install"], "2026-10-01 10:00")
        self.assertEqual(info["com.c.d"]["first_install"], "")

    def test_ad_activity(self):
        self.assertTrue(adbust.ad_like_activity(
            "com.google.android.gms.ads.AdActivity"))
        self.assertTrue(adbust.ad_like_activity(
            "com.applovin.adview.AppLovinFullscreenActivity"))
        self.assertFalse(adbust.ad_like_activity("com.whatsapp.Main"))

    def test_valid_pkg(self):
        self.assertTrue(adbust.valid_pkg("com.foo.bar_2"))
        for bad in ("", "foo", "com..x", "1com.x", "com.x; rm -rf"):
            self.assertFalse(adbust.valid_pkg(bad), bad)


class Scoring(unittest.TestCase):
    def setUp(self):
        self.phone = fakephone.make()
        self.db, _ = temp_db()

    def rows(self):
        rows, ctx = adbust.gather(self.phone, None, self.db)
        return {r["pkg"]: r for r in rows}, ctx

    def test_gather_scores(self):
        rows, ctx = self.rows()
        self.assertEqual(set(rows), set(self.phone.packages))
        ad = rows[ADWARE]
        # overlay 3 + hidden 2 + sideloaded 3 + recent 1 + name 2
        self.assertEqual(ad["score"], 11)
        self.assertEqual(ad["severity"], "HIGH")
        self.assertEqual(rows[GOOD]["score"], 0)
        self.assertIn(ADMIN, ctx["admins"])
        self.assertIn("registered as device admin", rows[ADMIN]["why"])
        self.assertIn("accessibility service enabled", rows[ACCESS]["why"])
        self.assertIn("can read all notifications", rows[CLEANER]["why"])
        self.assertEqual(ad["installed"], "2026-10-15 09:12")

    def test_known_bad_is_severe_and_counted(self):
        self.db.add(ADWARE, "PDF Scanner", "adware", "uninstall")
        rows, _ = self.rows()
        self.assertEqual(rows[ADWARE]["severity"], "SEVERE")
        self.assertTrue(rows[ADWARE]["why"][0].startswith("IN BAD-APP"))
        self.assertEqual(self.db.get(ADWARE)["times_seen"], 1)
        self.rows()
        self.assertEqual(self.db.get(ADWARE)["times_seen"], 2)


class Actions(unittest.TestCase):
    def setUp(self):
        self.phone = fakephone.make()

    def test_declaw(self):
        done = self.phone.declaw(ACCESS)
        self.assertIn("accessibility revoked", done)
        self.assertNotIn(ACCESS, self.phone.secure[
            "enabled_accessibility_services"])
        self.assertIn("talkback", self.phone.secure[
            "enabled_accessibility_services"])
        self.phone.declaw(CLEANER)
        self.assertNotIn("enabled_notification_listeners", self.phone.secure)
        self.assertEqual(
            self.phone.appops[CLEANER]["SYSTEM_ALERT_WINDOW"], "deny")

    def test_uninstall_and_admin_fallback(self):
        self.assertEqual(self.phone.uninstall(ADWARE), "Success")
        self.assertNotIn(ADWARE, self.phone.packages)
        res = self.phone.uninstall(ADMIN, f"{ADMIN}/.AdminReceiver")
        self.assertIn("DEVICE_POLICY", res)
        self.assertIn("App info is now open", res)
        self.assertEqual(self.phone.started, [f"package:{ADMIN}"])

    def test_disable_enable(self):
        self.assertIn("disabled-user", self.phone.disable(CLEANER))
        self.phone.enable(CLEANER)
        self.assertNotIn(CLEANER, self.phone.disabled)

    def test_window_state(self):
        self.phone.focus = (ADWARE, f"{ADWARE}.AdActivity")
        self.phone.overlay_windows = {CLEANER}
        pkg, act, ov = self.phone.window_state()
        self.assertEqual(pkg, ADWARE)
        self.assertEqual(ov, {CLEANER})


class Database(unittest.TestCase):
    def test_shared_loaded_and_local_edits_survive_merge(self):
        db, d = temp_db([{"pkg": "com.bad.one", "category": "adware",
                          "action": "uninstall", "label": "Bad"}])
        self.assertEqual(db.get("com.bad.one")["source"], "shared")
        db.add("com.bad.one", "My label", "malware", "disable")
        self.assertEqual(db.get("com.bad.one")["source"], "shared+local")
        added, updated = db.merge([{"pkg": "com.bad.one", "label": "Up"}])
        self.assertEqual((added, updated), (0, 0))
        self.assertEqual(db.get("com.bad.one")["label"], "My label")

    def test_shared_rows_take_upstream_changes(self):
        db, _ = temp_db([{"pkg": "com.bad.one", "label": "Old"}])
        self.assertEqual(db.merge([{"pkg": "com.bad.one", "label": "New"}]),
                         (0, 1))
        self.assertEqual(db.get("com.bad.one")["label"], "New")

    def test_invalid_rows_skipped_and_rejected(self):
        db, _ = temp_db()
        self.assertEqual(db.merge([{"pkg": "nope"}, {"x": 1}, {"pkg": "a.b",
                                   "reports": "junk"}]), (1, 0))
        with self.assertRaises(adbust.DBError):
            db.add("not a package")

    def test_export_roundtrip(self):
        db, d = temp_db()
        db.add("com.bad.two", "Two", "popup ads", "declaw", "n")
        out = os.path.join(d, "out.json")
        self.assertEqual(db.export_json(out), 1)
        doc = json.load(open(out))
        self.assertEqual(doc["schema"], 1)
        self.assertEqual(doc["apps"][0]["action"], "declaw")
        db2, _ = temp_db()
        self.assertEqual(db2.import_file(out), (1, 0))
        self.assertEqual(db2.get("com.bad.two")["category"], "popup ads")

    def test_update_from_url_file_scheme(self):
        db, d = temp_db()
        src = os.path.join(d, "remote.json")
        json.dump({"apps": [{"pkg": "com.remote.bad"}]}, open(src, "w"))
        self.assertEqual(db.update_from_url("file://" + src), (1, 0))
        with self.assertRaises(adbust.DBError):
            db.update_from_url("file://" + os.path.join(d, "missing.json"))

    def test_bad_json(self):
        with self.assertRaises(adbust.DBError):
            adbust.load_db_json("{not json")
        with self.assertRaises(adbust.DBError):
            adbust.load_db_json('{"apps": 3}')

    def test_jobs_and_actions(self):
        db, _ = temp_db()
        phone = fakephone.make()
        job = db.start_job(phone)
        db.log_action(job, ADWARE, "uninstall", "Success")
        jobs = db.jobs()
        self.assertEqual(jobs[0]["n_actions"], 1)
        self.assertEqual(jobs[0]["model"], "Samsung SM-A546E")
        self.assertEqual(db.actions_for(job)[0]["result"], "Success")
        self.assertEqual(db.stats()["removed"], 1)


class Report(unittest.TestCase):
    def test_report(self):
        phone = fakephone.make()
        db, _ = temp_db()
        db.add(ADWARE)
        rows, ctx = adbust.gather(phone, None, db)
        text = adbust.build_report(phone, rows, ctx, ["x: declawed"])
        self.assertIn("Known-bad apps found: 1", text)
        self.assertIn(ADMIN, text)
        self.assertIn("ACTIONS TAKEN", text)


if __name__ == "__main__":
    unittest.main()
