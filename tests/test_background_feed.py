"""Offline tests for feeds.BackgroundFeed (slow sources refreshed off the request path)."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feeds  # noqa: E402


class BackgroundFeedTests(unittest.TestCase):
    def test_warming_up_then_serving(self):
        feed = feeds.BackgroundFeed(lambda: {"v": 1}, interval=60, max_stale=600, name="t")
        with self.assertRaises(feeds.FeedWarmingUp):
            feed()
        self.assertTrue(feed.refresh_once())
        out = feed()
        self.assertEqual(out["v"], 1)
        self.assertFalse(out["stale"])
        self.assertIn("fetched_at", out)

    def test_failure_keeps_last_good_snapshot(self):
        calls = []

        def fetch():
            calls.append(1)
            if len(calls) > 1:
                raise RuntimeError("source down")
            return {"v": 1}

        feed = feeds.BackgroundFeed(fetch, interval=60, max_stale=600, name="t")
        feed.refresh_once()
        self.assertFalse(feed.refresh_once())
        self.assertEqual(feed()["v"], 1)
        self.assertIn("source down", feed.last_error)

    def test_too_old_is_an_error(self):
        now = [1000.0]
        feed = feeds.BackgroundFeed(lambda: {"v": 1}, interval=60, max_stale=600, clock=lambda: now[0], name="t")
        feed.refresh_once()
        now[0] += 130
        self.assertTrue(feed()["stale"])
        now[0] += 600
        with self.assertRaises(RuntimeError):
            feed()

    def test_snapshot_survives_restart(self):
        path = os.path.join(tempfile.mkdtemp(), "snap.json")
        first = feeds.BackgroundFeed(lambda: {"v": 7}, interval=60, max_stale=600, state_path=path, name="t")
        first.refresh_once()
        restarted = feeds.BackgroundFeed(lambda: {"v": 8}, interval=60, max_stale=600, state_path=path, name="t")
        self.assertEqual(restarted()["v"], 7)          # served before any refresh
        self.assertEqual(restarted.current(), {"v": 7})

    def test_requests_never_wait_for_a_slow_refresh(self):
        release, values = threading.Event(), iter([{"v": 1}, {"v": 2}])

        def fetch():
            value = next(values)
            if value["v"] == 2:
                release.wait(5)                          # a refresh that takes "minutes"
            return value

        feed = feeds.BackgroundFeed(fetch, interval=60, max_stale=600, name="t")
        feed.refresh_once()
        worker = threading.Thread(target=feed.refresh_once)
        worker.start()
        started = time.monotonic()
        self.assertEqual(feed()["v"], 1)                 # old snapshot, immediately
        self.assertLess(time.monotonic() - started, 0.5)
        release.set()
        worker.join()
        self.assertEqual(feed()["v"], 2)

    def test_background_thread_refreshes(self):
        done = threading.Event()

        def fetch():
            done.set()
            return {"v": 1}

        feed = feeds.BackgroundFeed(fetch, interval=60, max_stale=600, name="t").start()
        self.assertTrue(done.wait(5))
        feed.stop()


class WarmingRouteTests(unittest.TestCase):
    def test_route_answers_503_while_warming(self):
        from tests import test_plans_samples as t   # sets env and imports the app
        feed = feeds.BackgroundFeed(lambda: {"v": 1}, interval=60, max_stale=600, name="t")
        previous = feeds._PROVIDERS.get("spr-stockpile")
        feeds._PROVIDERS["spr-stockpile"] = feed
        try:
            from fastapi.testclient import TestClient
            r = TestClient(t.backend.app).get("/api/v1/batch/spr-stockpile", headers=t.GW)
            self.assertEqual(r.status_code, 503)
            self.assertEqual(r.json()["detail"], "Feed warming up")
        finally:
            if previous is None:
                feeds._PROVIDERS.pop("spr-stockpile", None)
            else:
                feeds._PROVIDERS["spr-stockpile"] = previous


if __name__ == "__main__":
    unittest.main()
