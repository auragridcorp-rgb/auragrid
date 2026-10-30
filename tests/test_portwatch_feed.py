"""Offline tests for the PortWatch provider and the labeled sample examples (no network)."""
import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ercot_feed  # noqa: E402
import portwatch_feed as pw  # noqa: E402
import sample_examples  # noqa: E402
from tests.test_ercot_feed import FakeSession as FakeErcotSession  # noqa: E402

PORTS = [{"portid": "p1", "portname": "Big", "country": "A", "ISO3": "AAA", "LOCODE": "AABIG"},
         {"portid": "p2", "portname": "Mid", "country": "B", "ISO3": "BBB", "LOCODE": "BBMID"},
         {"portid": "p3", "portname": "Small", "country": "C", "ISO3": "CCC", "LOCODE": "CCSML"}]
BASE_DAILY = {"p1": 100, "p2": 50, "p3": 10}       # normal calls/day
RECENT_DAILY = {"p1": 50, "p2": 50, "p3": 20}      # p1 disrupted this week


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class FakeArcGIS:
    def __init__(self, fail_first=0):
        self.calls, self.fail_first = [], fail_first

    def get(self, url, params=None, timeout=None):
        return self.post(url, data=params, timeout=timeout)

    def post(self, url, data=None, timeout=None):
        self.calls.append((url, data))
        if self.fail_first:
            self.fail_first -= 1
            return FakeResponse({"error": {"code": 400, "message": "timeout"}})
        stats = json.loads(data["outStatistics"]) if "outStatistics" in data else None
        where = data.get("where", "")
        if url == pw.PORTS_DB:
            return FakeResponse({"features": [{"attributes": p} for p in PORTS]})
        if stats and stats[0]["statisticType"] == "max":
            return FakeResponse({"features": [{"attributes": {"d": "2026-10-23"}}]})
        recent = "2026-10-17" in where
        days = 7 if recent else 90
        if url == pw.CHOKEPOINTS_DAILY:
            n = 70 if recent else 900
            return FakeResponse({"features": [{"attributes": {"portname": "Suez Canal", "n": n, "tanker": n // 2,
                                                              "container": n // 4, "cap": n * 1000, "days": days}}]})
        assert "portid IN" in where, "daily port queries must be filtered by port id"
        rows = []
        for p in PORTS:
            if "'%s'" % p["portid"] not in where:
                continue
            per_day = (RECENT_DAILY if recent else BASE_DAILY)[p["portid"]]
            rows.append({"portid": p["portid"], "calls": per_day * days, "imp": per_day * days * 1000,
                         "exp": per_day * days * 500, "tanker": days, "container": days * 2, "days": days})
        return FakeResponse({"features": [{"attributes": r} for r in rows]})


class PortWatchTests(unittest.TestCase):
    def build(self, session):
        with mock.patch.object(pw, "TOP_N", 3):
            last = pw.latest_date(session)
            return pw.build_snapshot(session, last, pw.rank_ports(session, last))

    def test_snapshot(self):
        snap = self.build(FakeArcGIS())
        self.assertEqual(snap["data_as_of"], "2026-10-23")
        self.assertEqual(snap["recent_window"], ["2026-10-17", "2026-10-23"])
        self.assertEqual(snap["baseline_window"], ["2026-07-19", "2026-10-16"])
        # ranked by baseline traffic, so the disrupted port keeps rank 1
        self.assertEqual([p["port"] for p in snap["ports"]], ["Big", "Mid", "Small"])
        big = snap["ports"][0]
        self.assertEqual(big["activity_ratio"], 0.5)
        self.assertEqual(big["port_calls_7d"], 350)
        self.assertEqual(snap["ports"][2]["activity_ratio"], 2.0)
        self.assertEqual(snap["global"]["activity_ratio"], round((50 + 50 + 20) / (100 + 50 + 10), 3))
        self.assertEqual(snap["chokepoints"][0]["transit_ratio"], 1.0)
        self.assertIn("free of charge from the IMF", snap["attribution"]["free_from_source"])

    def test_retries_transient_errors(self):
        with mock.patch.object(pw.time, "sleep"):
            snap = self.build(FakeArcGIS(fail_first=2))
        self.assertEqual(len(snap["ports"]), 3)

    def test_incremental_refresh_and_saved_ranking(self):
        import tempfile
        session, builds, ranks = FakeArcGIS(), [], []

        def build(s, last, ranked):
            builds.append((last, ranked))
            return {"data_as_of": last.isoformat()}

        def rank(s, last):
            ranks.append(last)
            return ["p1", "p2"]

        state = os.path.join(tempfile.mkdtemp(), "ranking.json")
        inc = pw.IncrementalSnapshot(session, build=build, rank=rank, state_path=state)
        inc()
        inc()
        self.assertEqual((len(builds), len(ranks)), (1, 1))     # same week: no rebuild, no re-rank
        restarted = pw.IncrementalSnapshot(session, build=build, rank=rank, state_path=state)
        restarted()
        self.assertEqual(len(ranks), 1)                         # ranking survived the restart
        self.assertEqual(builds[-1][1], ["p1", "p2"])

    def test_disabled_without_permission_flag(self):
        os.environ.pop("AURAGRID_PORTWATCH_ENABLED", None)
        self.assertFalse(pw.register_from_env())


class ExampleSchemaTests(unittest.TestCase):
    """Examples must have exactly the keys the real providers produce."""

    DATA_KEYED = {"$.trading_hubs", "$.load_zones"}   # dicts keyed by names (data), not schema

    def assert_same_shape(self, example, real, path="$"):
        if path in self.DATA_KEYED:
            self.assertTrue(all(isinstance(v, float) for v in example.values()), path)
        elif isinstance(real, dict):
            self.assertEqual(sorted(example), sorted(real), path)
            for k in real:
                self.assert_same_shape(example[k], real[k], path + "." + k)
        elif isinstance(real, list) and real and isinstance(real[0], dict):
            self.assert_same_shape(example[0], real[0], path + "[0]")

    def cached(self, fetch):
        import feeds
        return feeds.CachedFeed(fetch, 60, 600)()

    def test_ercot_example_matches(self):
        client = ercot_feed.ERCOTClient("k", session=FakeErcotSession())
        real = self.cached(lambda: ercot_feed.build_snapshot(client))
        self.assert_same_shape(sample_examples.EXAMPLES["ercot-lmp"], real)

    def test_port_example_matches(self):
        with mock.patch.object(pw, "TOP_N", 3):
            session = FakeArcGIS()
            last = pw.latest_date(session)
            real = self.cached(lambda: pw.build_snapshot(session, last, pw.rank_ports(session, last)))
        self.assert_same_shape(sample_examples.EXAMPLES["port-congestion-index"], real)

    def test_examples_are_labeled(self):
        body = sample_examples.example_body("ercot-lmp")
        self.assertEqual((body["mode"], body["example"], body["synthetic"]), ("schema_example", True, True))
        self.assertIn("synthetic", body["notice"])
        self.assertIsNone(sample_examples.example_body("naval-escalation"))


if __name__ == "__main__":
    unittest.main()
