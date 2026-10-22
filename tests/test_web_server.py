import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "web"))
import server  # noqa: E402

TEST = "io.github.thvjq.adbusttest.pdfscanner"


class Store(unittest.TestCase):
    def setUp(self):
        self.s = server.Store(Path(tempfile.mkdtemp()) / "w.db")

    def test_report_shows_up_in_badapps(self):
        n = self.s.report([{"pkg": TEST, "label": "PDF", "how": "popup"}],
                          "client-aaaa", "1.2.3.4")
        self.assertEqual(n, 1)
        apps = self.s.badapps()["apps"]
        self.assertEqual(apps[0]["pkg"], TEST)
        self.assertEqual(apps[0]["reports"], 1)
        self.assertEqual(apps[0]["action"], "uninstall")

    def test_one_count_per_person(self):
        for _ in range(3):
            self.s.report([{"pkg": TEST}], "client-aaaa", "1.2.3.4")
        self.s.report([{"pkg": TEST}], "client-bbbb", "5.6.7.8")
        self.assertEqual(self.s.badapps()["apps"][0]["reports"], 2)

    def test_system_and_junk_refused(self):
        n = self.s.report([{"pkg": "com.android.settings"},
                           {"pkg": "com.samsung.android.dialer"},
                           {"pkg": "android"}, {"pkg": "not a pkg"},
                           {"pkg": "x; rm -rf /"}], "client-aaaa", "1.2.3.4")
        self.assertEqual(n, 0)
        self.assertEqual(self.s.badapps()["apps"], [])

    def test_daily_limit_per_client(self):
        items = [{"pkg": f"com.spam.app{i}"} for i in range(250)]
        self.s.report(items[:100], "client-aaaa", "1.2.3.4")
        self.s.report(items[100:200], "client-aaaa", "1.2.3.4")
        self.assertEqual(self.s.report(items[200:], "client-aaaa",
                                       "1.2.3.4"), 0)
        self.assertEqual(len(self.s.badapps()["apps"]), 200)

    def test_apps_are_date_stamped(self):
        self.s.report([{"pkg": TEST}], "client-aaaa", "1.2.3.4")
        a = self.s.badapps()["apps"][0]
        self.assertRegex(a["added_at"], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ$")
        self.assertEqual(a["added"], a["added_at"][:10])

    def test_usage_counts_times_and_browsers(self):
        for c in ("client-aaaa", "client-aaaa", "client-bbbb"):
            self.assertTrue(self.s.track("visit", c))
        self.s.track("connect", "client-aaaa")
        self.s.report([{"pkg": TEST}], "client-aaaa", "1.2.3.4")
        (day, evs), = self.s.usage(1).items()
        self.assertEqual(evs["visit"], [3, 2])
        self.assertEqual(evs["connect"], [1, 1])
        self.assertEqual(evs["new_apps"][0], 1)

    def test_usage_refuses_junk_and_floods(self):
        self.assertFalse(self.s.track("drop table", "client-aaaa"))
        for _ in range(server.MAX_USAGE_PER_CLIENT_DAY):
            self.s.track("visit", "client-aaaa")
        self.assertFalse(self.s.track("visit", "client-aaaa"))

    def test_good_apps_leave_the_bad_list(self):
        self.s.report([{"pkg": TEST, "label": "PDF"}], "client-aaaa", "ip")
        self.s.report([{"pkg": "com.spam.other"}], "client-aaaa", "ip")
        n = self.s.mark_good([{"pkg": TEST, "good": True},
                              {"pkg": "not a pkg"}], "client-bbbb")
        self.assertEqual(n, 1)
        bad = [a["pkg"] for a in self.s.badapps()["apps"]]
        self.assertEqual(bad, ["com.spam.other"])
        g = self.s.goodapps()["apps"]
        self.assertEqual((g[0]["pkg"], g[0]["label"], g[0]["reports"]),
                         (TEST, "PDF", 1))
        # Un-marking puts it straight back on the bad list.
        self.s.mark_good([{"pkg": TEST, "good": False}], "client-cccc")
        self.assertEqual(len(self.s.badapps()["apps"]), 2)
        self.assertEqual(self.s.goodapps()["apps"], [])

    def test_manual_removals_are_listed(self):
        self.s.report([{"pkg": TEST, "how": "manual"}], "client-aaaa", "ip")
        self.assertEqual(self.s.badapps()["apps"][0]["pkg"], TEST)
        how = self.s.conn.execute("SELECT how FROM reports").fetchone()[0]
        self.assertEqual(how, "manual")

    def test_desktop_adbust_can_read_it(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import adbust
        self.s.report([{"pkg": TEST, "label": "PDF"}], "client-aaaa", "ip")
        import json
        apps = adbust.load_db_json(json.dumps(self.s.badapps()))
        self.assertEqual(apps[0]["pkg"], TEST)


if __name__ == "__main__":
    unittest.main()
