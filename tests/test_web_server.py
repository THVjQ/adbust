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

    def test_desktop_adbust_can_read_it(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import adbust
        self.s.report([{"pkg": TEST, "label": "PDF"}], "client-aaaa", "ip")
        import json
        apps = adbust.load_db_json(json.dumps(self.s.badapps()))
        self.assertEqual(apps[0]["pkg"], TEST)


if __name__ == "__main__":
    unittest.main()
