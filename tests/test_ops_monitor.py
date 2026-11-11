"""Operator monitoring: traffic log parsing, path classification, admin-only routes."""
import gzip
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
import ops_monitor  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc).timestamp()


def line(day, ip, path, ua="agent/1.0", method="GET"):
    return '%s 10:00:00.000 [INF] PRXY: %s - - "%s %s HTTP/1.1" "" "%s"\n' % (day, ip, method, path, ua)


def classify(path):
    return {"/api/v1/batch/btc-fees": "paid", "/llms.txt": "discovery"}.get(path, "noise")


class TrafficLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "aperture.log")

    def make(self, text, **kw):
        with open(self.path, "w") as f:
            f.write(text)
        return ops_monitor.TrafficLog(self.path, classify, self_ips={"9.9.9.9"}, min_interval=0,
                                      clock=lambda: NOW, **kw)

    def test_counts_categories_clients_and_excludes_self(self):
        log = self.make(line("2026-11-11", "1.1.1.1", "/api/v1/batch/btc-fees?x=1")
                        + line("2026-11-11", "1.1.1.1", "/llms.txt")
                        + line("2026-11-10", "2.2.2.2", "/.env")
                        + line("2026-11-11", "9.9.9.9", "/llms.txt")
                        + "2026-11-08 00:55:17.350 [INF] APER: Starting the server, listening on 0.0.0.0:443.\n")
        s = log.summary(7)
        self.assertEqual(s["totals"], {"paid": 1, "free": 0, "quote": 0, "discovery": 1, "noise": 1})
        self.assertEqual(s["clients"]["real"], 1)
        self.assertEqual(s["clients"]["noise"], 1)
        self.assertEqual(s["self_requests"], 1)
        self.assertEqual(s["aperture_starts"], ["2026-11-08 00:55:17"])
        self.assertEqual(len(s["daily"]), 7)
        self.assertEqual(s["daily"][-1]["day"], "2026-11-11")
        self.assertEqual(s["daily"][-1]["clients"], 1)
        self.assertEqual([r["path"] for r in s["recent"]], ["/llms.txt", "/api/v1/batch/btc-fees"])

    def test_window_and_incremental_reads(self):
        log = self.make(line("2026-10-01", "1.1.1.1", "/llms.txt"))
        self.assertEqual(log.summary(7)["totals"]["discovery"], 0)   # outside the window
        with open(self.path, "a") as f:
            f.write(line("2026-11-11", "3.3.3.3", "/llms.txt"))
            f.write('2026-11-11 10:00:01.000 [INF] PRXY: 4.4.4.4 - - "GET /llms')   # unfinished line
        self.assertEqual(log.summary(7)["totals"]["discovery"], 1)
        with open(self.path, "a") as f:
            f.write('.txt HTTP/1.1" "" "ua"\n')
        self.assertEqual(log.summary(7)["totals"]["discovery"], 2)

    def test_rotation_reads_the_gzipped_previous_file(self):
        log = self.make(line("2026-11-11", "1.1.1.1", "/llms.txt"))
        self.assertEqual(log.summary(7)["totals"]["discovery"], 1)
        with gzip.open(self.path + ".1.gz", "wt") as f:
            f.write(line("2026-11-10", "5.5.5.5", "/llms.txt") + line("2026-11-11", "1.1.1.1", "/llms.txt"))
        os.remove(self.path)
        with open(self.path, "w") as f:
            f.write(line("2026-11-11", "6.6.6.6", "/api/v1/batch/btc-fees"))
        s = log.summary(7)
        self.assertEqual(s["totals"]["discovery"], 2)
        self.assertEqual(s["totals"]["paid"], 1)


class ClassifyAndRouteTests(unittest.TestCase):
    def test_classify_path(self):
        c = t.backend.classify_path
        self.assertEqual(c("/api/v1/batch/btc-fees"), "paid")
        self.assertEqual(c("/api/v1/batch/maritime-weather"), "free")
        self.assertEqual(c("/api/v1/price/batch/btc-fees"), "quote")
        self.assertEqual(c("/agent-manifest.json"), "discovery")
        # Paths that only normalize to a feed name are not API traffic.
        self.assertEqual(c("/btc-fees"), "noise")
        self.assertEqual(c("/batch/btc-fees"), "noise")
        self.assertEqual(c("/.env"), "noise")

    def test_monitoring_routes_are_admin_only(self):
        client = TestClient(t.backend.app)
        for path in ("/api/proxy/health", "/api/proxy/traffic"):
            self.assertEqual(client.get(path).status_code, 401, path)
            self.assertEqual(client.get(path, auth=("op", "wrong")).status_code, 401, path)

    def test_health_reports_feeds_and_alerts(self):
        r = TestClient(t.backend.app).get("/api/proxy/health", auth=("op", "pw"))
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual({f["feed"] for f in body["feeds"]}, set(t.backend.JOBS))
        self.assertIsInstance(body["alerts"], list)
        self.assertEqual(body["ok"], not body["alerts"])
