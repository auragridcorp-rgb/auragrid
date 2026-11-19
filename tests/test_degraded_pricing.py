"""Phase 4 fail-safe: a degraded (stale or missing) paid feed is priced at 0 sats."""
import os
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
import gen_aperture  # noqa: E402
import jobs  # noqa: E402
import pricer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pass_support import load_access, load_plans  # noqa: E402

NOW = 1_800_000_000.0


class FakeStore:
    def __init__(self, newest):
        self.newest = newest   # feed -> retrieved_at of its newest snapshot

    def latest_retrieved_at(self, key):
        return self.newest.get(key)


class FeedDegradedTests(unittest.TestCase):
    def test_fresh_stale_missing_and_unknown(self):
        store = FakeStore({"btc-fees": NOW - 60, "ln-network": NOW - 4 * 3600})
        clock = lambda: NOW  # noqa: E731
        self.assertFalse(jobs.feed_degraded(store, "btc-fees", clock))       # 1 min old, limit 15 min
        self.assertTrue(jobs.feed_degraded(store, "ln-network", clock))      # 4 h old, limit 3 h
        self.assertTrue(jobs.feed_degraded(FakeStore({}), "btc-fees", clock))  # never fetched
        self.assertFalse(jobs.feed_degraded(store, "not-a-feed", clock))     # no limits: never degraded


class StoreFreshnessTests(unittest.TestCase):
    def test_unchanged_upstream_data_does_not_count_as_fresh(self):
        from store import Store
        now = [NOW - 3600]
        store = Store(os.path.join(tempfile.mkdtemp(), "feeds.db"), clock=lambda: now[0])
        store.save("btc-fees", {"x": 1}, "2026-11-11T09:00:00+00:00")
        now[0] = NOW
        store.save("btc-fees", {"x": 1}, "2026-11-11T09:00:00+00:00")   # same as_of: not stored
        self.assertTrue(jobs.feed_degraded(store, "btc-fees", lambda: NOW))   # 1 h > 15 min
        store.save("btc-fees", {"x": 2}, "2026-11-11T10:00:00+00:00")
        self.assertFalse(jobs.feed_degraded(store, "btc-fees", lambda: NOW))


class BtcAsOfTests(unittest.TestCase):
    def test_btc_fees_as_of_is_the_observation_time(self):
        import btc_feed  # noqa: F401  (registers the source)
        import feeds
        snap = {"observed_at": "2026-11-11T09:41:00+00:00", "tip": {"block_time_utc": "2026-11-11T09:00:00+00:00"}}
        self.assertEqual(feeds.as_of_for("btc-fees", snap), "2026-11-11T09:41:00+00:00")
        del snap["observed_at"]   # snapshots stored before the change
        self.assertEqual(feeds.as_of_for("btc-fees", snap), "2026-11-11T09:00:00+00:00")


class DegradedPricingTests(unittest.TestCase):
    def setUp(self):
        cfg = {"services": [
            {"name": "free-feeds", "pathregexp": "^/api/v1/batch/earthquakes$", "price": 0},
            {"name": "lightning-operator", "pathregexp": "^/api/v1/bundle/(btc-fees|ln-network)$",
             "price": 5000, "timeout": 2592000},
            {"name": "batch-btc-fees", "pathregexp": "^/api/v1/batch/btc-fees$", "price": 50, "timeout": 3600,
             "dynamicprice": {"enabled": True}},
            {"name": "energy", "pathregexp": "^/api/v1/energy/grid$", "price": 70, "timeout": 3600,
             "dynamicprice": {"enabled": True}},
        ]}
        d = tempfile.mkdtemp()
        self.ap = os.path.join(d, "aperture.yaml")
        self.pr = os.path.join(d, "pricing.yaml")
        with open(self.ap, "w") as f:
            yaml.safe_dump(cfg, f)
        with open(self.pr, "w") as f:
            yaml.safe_dump({"endpoints": {}}, f)
        self.bad = set()
        self.engine = pricer.PriceEngine(self.ap, self.pr, degraded=lambda key: key in self.bad)

    def test_fresh_feeds_keep_their_price(self):
        self.assertEqual(self.engine.sats_for("/api/v1/batch/btc-fees"), 50)
        self.assertEqual(self.engine.sats_for("/api/v1/bundle/btc-fees"), 5000)
        self.assertNotIn("degraded", self.engine.quote("/api/v1/batch/btc-fees"))

    def test_degraded_feed_is_free_on_its_hourly_path(self):
        self.bad = {"btc-fees"}
        self.assertEqual(self.engine.sats_for("/api/v1/batch/btc-fees"), 0)
        # The bundle is statically priced in Aperture (no dynamicprice), so its quote
        # must keep matching what Aperture charges.
        self.assertEqual(self.engine.sats_for("/api/v1/bundle/btc-fees"), 5000)
        q = self.engine.quote("/api/v1/batch/btc-fees")
        self.assertEqual((q["price_sats"], q["pricing"], q["degraded"]), (0, "degraded_free", True))

    def test_slash_feed_keys_are_not_mistaken_for_plan_prefixes(self):
        self.assertEqual(pricer.PriceEngine.feed_key("/api/v1/energy/grid"), "energy/grid")
        self.assertEqual(pricer.PriceEngine.feed_key("/api/v1/bundle/ln-network"), "ln-network")
        self.bad = {"energy/grid"}
        self.assertEqual(self.engine.sats_for("/api/v1/energy/grid"), 0)

    def test_free_rules_are_not_reported_as_degraded(self):
        self.bad = {"earthquakes"}
        self.assertNotEqual(self.engine.quote("/api/v1/batch/earthquakes")["pricing"], "degraded_free")

    def test_a_failing_freshness_check_charges_normally(self):
        def boom(_key):
            raise RuntimeError("db locked")
        engine = pricer.PriceEngine(self.ap, self.pr, degraded=boom)
        self.assertEqual(engine.sats_for("/api/v1/batch/btc-fees"), 50)

    def test_store_check_caches_per_key(self):
        calls = []

        class CountingStore(FakeStore):
            def latest_retrieved_at(self, key):
                calls.append(key)
                return super().latest_retrieved_at(key)

        check = pricer.store_degraded_check(CountingStore({}), cache_seconds=60)
        self.assertTrue(check("btc-fees"))
        self.assertTrue(check("btc-fees"))
        self.assertEqual(calls, ["btc-fees"])


class ApertureConfigTests(unittest.TestCase):
    def test_hourly_paid_services_use_the_pricer_and_bundles_stay_static(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = yaml.safe_load(open(os.path.join(here, "aperture.base.yaml")))
        pricing = os.path.join(here, "pricing.yaml")
        cfg = gen_aperture.build(base, load_plans(pricing), 100000.0, None, load_access(pricing))
        bundles = {p.name for p in load_plans(pricing) if p.kind == "bundle"}
        hourly = [s for s in cfg["services"] if s.get("price") and s["name"] not in bundles]
        self.assertTrue(hourly)
        for svc in hourly:
            self.assertTrue((svc.get("dynamicprice") or {}).get("enabled"), svc["name"])
        # Aperture scopes dynamically priced tokens to one path; a bundle token must
        # work on every path in the bundle, so bundles keep a static price.
        for svc in cfg["services"]:
            if svc["name"] in bundles:
                self.assertNotIn("dynamicprice", svc)


class ManifestTests(unittest.TestCase):
    def test_manifest_marks_degraded_paid_feeds(self):
        engine = t.backend.engine
        saved = engine.degraded, engine._rules
        # The frozen fixture config predates dynamicprice; enable it for this check.
        engine._rules = [(rx, {**svc, "dynamicprice": {"enabled": True}}) for rx, svc in engine._rules]
        engine.degraded = lambda key: True
        try:
            m = TestClient(t.backend.app).get("/agent-manifest.json").json()
        finally:
            engine.degraded, engine._rules = saved
        paid = [e for e in m["endpoints"] if e["access"] == "paid"]
        self.assertTrue(paid)
        for e in paid:
            self.assertEqual((e["status"], e["price_sats"]), ("stale", 0), e["path"])
