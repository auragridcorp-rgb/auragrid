"""Offline tests for store.py (snapshot store) and per-response provenance."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (shared env first)
import feeds  # noqa: E402
from store import Store  # noqa: E402


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.now = [1_800_000_000.0]
        self.s = Store(os.path.join(tempfile.mkdtemp(), "f.db"), raw_days=7, clock=lambda: self.now[0])

    def test_save_latest_and_dedupe(self):
        self.assertTrue(self.s.save("x", {"v": 1}, "2026-10-01"))
        self.now[0] += 60
        self.assertFalse(self.s.save("x", {"v": 1}, "2026-10-01"))   # same as_of: not stored again
        self.now[0] += 60
        self.assertTrue(self.s.save("x", {"v": 2}, "2026-10-02"))
        snap = self.s.latest("x")
        self.assertEqual((snap.data, snap.as_of), ({"v": 2}, "2026-10-02"))
        self.assertIsNone(self.s.latest("missing"))

    def test_failure_never_overwrites_and_is_counted(self):
        self.s.save("x", {"v": 1}, "a")
        self.s.record_failure("x", "boom")
        self.s.record_failure("x", "boom again")
        self.assertEqual(self.s.latest("x").data, {"v": 1})
        st = self.s.status("x")
        self.assertEqual((st["consecutive_failures"], st["last_error"]), (2, "boom again"))
        self.s.save("x", {"v": 2}, "b")
        self.assertEqual(self.s.status("x")["consecutive_failures"], 0)

    def test_retention_raw_7_days_daily_forever(self):
        self.s.save("x", {"v": 1}, "old")
        self.now[0] += 8 * 86400
        self.s.save("x", {"v": 2}, "new")
        self.assertEqual(self.s.prune(), 1)
        self.assertEqual(len(self.s.daily_history("x")), 2)            # both days kept
        self.s.vacuum()
        self.assertEqual(self.s.latest("x").data, {"v": 2})

    def test_delayed_copy(self):
        self.s.save("x", {"v": 1}, "a")
        self.now[0] += 600
        self.s.save("x", {"v": 2}, "b")
        self.assertEqual(self.s.latest_before("x", self.now[0] - 300).data, {"v": 1})


class ProvenanceTests(unittest.TestCase):
    def test_every_feed_response_carries_provenance(self):
        from fastapi.testclient import TestClient
        feeds.describe("maritime-weather", source="NOAA NDBC", url="https://example.gov/obs",
                       terms_url="https://example.gov/terms", attribution="Source: NOAA",
                       as_of=lambda snap: snap["observed_between"][1])
        feeds.provider("maritime-weather")(feeds.CachedFeed(
            lambda: {"observed_between": ["2026-10-30T10:00", "2026-10-30T12:00"], "stations": 1}, 60, 600))
        try:
            body = TestClient(t.backend.app).get("/api/v1/batch/maritime-weather").json()
        finally:
            feeds._PROVIDERS.pop("maritime-weather", None)
        p = body["provenance"]
        self.assertEqual((p["source"], p["as_of"], p["terms_url"]), ("NOAA NDBC", "2026-10-30T12:00", "https://example.gov/terms"))
        self.assertTrue(p["retrieved_at"])
        self.assertIs(body["stale"], False)
        self.assertNotIn("fetched_at", body["data"])


if __name__ == "__main__":
    unittest.main()
