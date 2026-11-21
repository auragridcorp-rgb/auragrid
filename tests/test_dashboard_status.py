"""Dashboard Node API tile: only an LND failure (HTTP 502) may say the node is unreachable."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
from fastapi.testclient import TestClient  # noqa: E402


class DashboardStatusTests(unittest.TestCase):
    def setUp(self):
        r = TestClient(t.backend.app).get(
            "/", auth=(os.environ["AURAGRID_ADMIN_USER"], os.environ["AURAGRID_ADMIN_PASSWORD"]))
        self.assertEqual(r.status_code, 200)
        self.html = r.text

    def test_failures_are_classified(self):
        for snippet in ('err.kind = "offline"', 'r.status === 401 ? "auth"', 'r.status === 502 ? "lnd"'):
            self.assertIn(snippet, self.html)

    def test_only_lnd_failures_mark_the_node_unreachable(self):
        self.assertIn('setStatus(err.kind === "lnd" ? "lnd" : "unknown", failureLabel(err))', self.html)
        self.assertNotIn("setStatus(false", self.html)

    def test_lnd_outage_is_a_502(self):
        class Down:
            def __getattr__(self, name):
                def fail(*a, **k):
                    raise ConnectionError("lnd down")
                return fail
        saved, t.backend.lnd = t.backend.lnd, Down()
        t.backend._cache["t"] = 0.0
        try:
            r = TestClient(t.backend.app).get(
                "/api/proxy/info", auth=(os.environ["AURAGRID_ADMIN_USER"], os.environ["AURAGRID_ADMIN_PASSWORD"]))
            self.assertEqual(r.status_code, 502)
        finally:
            t.backend.lnd = saved
            t.backend._cache["t"] = 0.0


if __name__ == "__main__":
    unittest.main()
