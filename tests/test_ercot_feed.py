"""Offline tests for the ERCOT ercot-lmp provider (fake HTTP session, no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ercot_feed  # noqa: E402
import feeds  # noqa: E402

FIELDS = [{"name": "SCEDTimestamp"}, {"name": "repeatHourFlag"}, {"name": "settlementPoint"}, {"name": "LMP"}]
STAMP = "2026-10-30T06:35:12"


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)


class FakeSession:
    """Two pages of row arrays for STAMP, plus a token endpoint."""

    def __init__(self, fail=False):
        self.gets, self.posts, self.fail = [], [], fail

    def post(self, url, data=None, timeout=None):
        self.posts.append(data)
        return FakeResponse({"id_token": "tok", "expires_in": "3600"})

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append((params, headers))
        if self.fail:
            return FakeResponse({"message": "down"}, 503)
        if params.get("size") == 1:
            return FakeResponse({"fields": FIELDS, "data": [[STAMP, False, "HB_HOUSTON", 31.5]]})
        page = params["page"]
        data = ([[STAMP, False, "HB_HOUSTON", 31.5], [STAMP, False, "LZ_NORTH", 28.0]] if page == 1
                else [[STAMP, False, "NODE_A", -2.25]])
        return FakeResponse({"fields": FIELDS, "data": data, "_meta": {"totalPages": 2}})


class ErcotFeedTests(unittest.TestCase):
    def test_snapshot_parses_all_pages_of_latest_interval(self):
        session = FakeSession()
        client = ercot_feed.ERCOTClient("key", "user@example.com", "pw", session=session)
        snap = ercot_feed.build_snapshot(client)
        self.assertEqual(snap["sced_timestamp"], STAMP)
        self.assertEqual(snap["settlement_points"], 3)
        self.assertEqual(snap["trading_hubs"], {"HB_HOUSTON": 31.5})
        self.assertEqual(snap["load_zones"], {"LZ_NORTH": 28.0})
        self.assertEqual(snap["summary"]["min_lmp"], -2.25)
        self.assertEqual([p["settlement_point"] for p in snap["prices"]], ["HB_HOUSTON", "LZ_NORTH", "NODE_A"])
        params = [g[0] for g in session.gets]
        self.assertEqual(params[1]["SCEDTimestampFrom"], STAMP)
        self.assertEqual(params[1]["SCEDTimestampTo"], STAMP)
        headers = session.gets[0][1]
        self.assertEqual(headers["Ocp-Apim-Subscription-Key"], "key")
        self.assertEqual(headers["Authorization"], "Bearer tok")
        self.assertEqual(len(session.posts), 1)   # token reused across calls

    def test_object_rows_and_key_only_auth(self):
        session = FakeSession()
        client = ercot_feed.ERCOTClient("key", session=session)
        report = {"fields": FIELDS, "data": [{"SCEDTimestamp": STAMP, "repeatHourFlag": True,
                                              "settlementPoint": "X", "LMP": 1}]}
        self.assertEqual(ercot_feed.rows(report)[0]["settlementPoint"], "X")
        client.get({"size": 1})
        self.assertNotIn("Authorization", session.gets[0][1])
        self.assertEqual(session.posts, [])

    def test_cache_and_stale_fallback(self):
        now = [1000.0]
        calls = []

        def fetch():
            calls.append(now[0])
            if len(calls) > 1:
                raise RuntimeError("ERCOT down")
            return {"sced_timestamp": STAMP}

        feed = feeds.CachedFeed(fetch, cache_seconds=60, max_stale_seconds=900, clock=lambda: now[0])
        self.assertFalse(feed()["stale"])
        now[0] += 30
        feed()
        self.assertEqual(len(calls), 1)                  # cached
        now[0] += 60
        self.assertTrue(feed()["stale"])                 # refresh failed: last good, flagged
        now[0] += 10
        feed()
        self.assertEqual(len(calls), 2)                  # no retry storm while backing off
        now[0] += 1000
        with self.assertRaises(Exception):               # too old: error, never ancient data
            feed()

    def test_not_registered_without_key(self):
        os.environ.pop("ERCOT_SUBSCRIPTION_KEY", None)
        self.assertFalse(ercot_feed.register_from_env())


if __name__ == "__main__":
    unittest.main()
