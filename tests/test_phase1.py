"""Machine-discovery Phase 1: stale gate, free samples, USD prices, dashboard truth."""
import os
import sys
import time
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
import pricer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

backend = t.backend
# Own token: the shared test token's plan is asserted elsewhere (first use sets it).
PAID_HEADERS = {**t.GW, "Authorization": "L402 AgEEbHNhdA:" + "cd" * 32}


def store_at(feed, data, as_of, age_seconds):
    """Store a snapshot as if it had been fetched `age_seconds` ago."""
    with backend.STORE._db() as conn:
        conn.execute("DELETE FROM raw WHERE feed=?", (feed,))
        conn.execute("DELETE FROM daily WHERE feed=?", (feed,))
    if feed == "btc-fees":
        data = {**data, "observed_at": as_of}   # btc-fees' as_of is its observation time
    saved = backend.STORE.clock
    backend.STORE.clock = lambda: time.time() - age_seconds
    try:
        backend.STORE.save(feed, data, as_of)
    finally:
        backend.STORE.clock = saved


def today():
    return datetime.now(timezone.utc).date().isoformat()


class StaleGateTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(backend.app)

    def tearDown(self):
        t.seed_store()

    def test_fresh_paid_feed_is_served(self):
        store_at("btc-fees", t.BTC, "2026-11-19T12:00:00+00:00", 60)
        r = self.client.get("/api/v1/batch/btc-fees", headers=PAID_HEADERS)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["stale"])

    def test_stale_paid_feed_answers_503_with_true_as_of_and_no_data(self):
        store_at("btc-fees", t.BTC, "2026-11-19T09:00:00+00:00", 3 * 3600)   # limit is 15 min
        before = backend.ledger.counter_total("stale_refusal", today())
        r = self.client.get("/api/v1/batch/btc-fees", headers=PAID_HEADERS)
        self.assertEqual(r.status_code, 503)
        body = r.json()
        self.assertTrue(body["stale"])
        self.assertEqual(body["as_of"], "2026-11-19T09:00:00+00:00")
        self.assertEqual(body["sample_url"], "/api/v1/batch/btc-fees-delayed")
        self.assertNotIn("data", body)
        self.assertEqual(r.headers["Retry-After"], "300")
        self.assertEqual(backend.ledger.counter_total("stale_refusal", today()), before + 1)

    def test_stale_gate_also_covers_plan_paths(self):
        store_at("btc-fees", t.BTC, "2026-11-19T09:00:00+00:00", 3 * 3600)
        r = self.client.get("/api/v1/bundle/btc-fees", headers=PAID_HEADERS)
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.json()["stale"])

    def test_paid_feed_without_any_snapshot_is_refused_as_stale(self):
        with backend.STORE._db() as conn:
            conn.execute("DELETE FROM raw WHERE feed='ln-network'")
            conn.execute("DELETE FROM daily WHERE feed='ln-network'")
        r = self.client.get("/api/v1/batch/ln-network", headers=PAID_HEADERS)
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.json()["stale"])
        self.assertIsNone(r.json()["as_of"])

    def test_pricer_issues_no_invoice_for_a_stale_feed(self):
        engine = backend.engine
        saved = engine._rules
        engine._rules = [(rx, {**svc, "dynamicprice": {"enabled": True}}) for rx, svc in engine._rules]
        try:
            store_at("btc-fees", t.BTC, "2026-11-19T09:00:00+00:00", 3 * 3600)
            self.assertEqual(engine.sats_for("/api/v1/batch/btc-fees"), 0)
            q = self.client.get("/api/v1/price/batch/btc-fees").json()
            self.assertTrue(q["degraded"])
            self.assertIn("503", q["note"])
        finally:
            engine._rules = saved


class SampleTests(unittest.TestCase):
    def test_every_paid_feed_has_a_free_sample(self):
        for slug, *_ in backend.PAID_CATALOG:
            key = backend.feed_key(slug)
            self.assertIn(key, backend.SAMPLE_PATHS, key)
            self.assertIn(backend.normalize_path(backend.SAMPLE_PATHS[key]), backend.FREE_PATHS)

    def test_quote_links_the_sample(self):
        q = TestClient(backend.app).get("/api/v1/price/batch/ln-network").json()
        self.assertEqual(q["sample_url"], "/api/v1/batch/ln-network-daily")


class PricingTests(unittest.TestCase):
    def test_live_pricing_is_usd_pegged_and_under_ten_cents(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        engine = pricer.PriceEngine(os.path.join(here, "aperture.yaml"), os.path.join(here, "pricing.yaml"))
        with engine._lock:
            engine._btc, engine._btc_time = 64000.0, time.monotonic()
        for path, usd in (("/api/v1/batch/btc-fees", 0.005), ("/api/v1/batch/ln-network", 0.01)):
            q = engine.quote(path)
            self.assertEqual((q["pricing"], q["usd_price"]), ("usd_pegged", usd), path)
            self.assertLess(q["price_sats"] / 1e8 * 64000, 0.10)
        bundle = engine.quote("/api/v1/bundle/btc-fees")
        self.assertEqual(bundle["pricing"], "static")
        self.assertLess(bundle["price_sats"] / 1e8 * 64000, 10.0)


class DashboardTruthTests(unittest.TestCase):
    def test_no_invented_services_and_all_time_invoice_count(self):
        class PagedLND(t.FakeLND):
            def get_invoices_page(self):
                return self.invoices, 11164

        old = backend.lnd
        backend.lnd, backend._cache["t"] = PagedLND([{"r_hash": "00", "value": "50", "state": "OPEN"}]), 0.0
        try:
            stats = TestClient(backend.app).get("/api/proxy/stats", auth=("op", "pw")).json()
        finally:
            backend.lnd, backend._cache["t"] = old, 0.0
        names = {row["service_name"] for row in stats["service_breakdown"]}
        self.assertNotIn("Mainnet Lightning Routing Liquidity", names)
        self.assertEqual((stats["transaction_count"], stats["transactions_analysed"]), (11164, 1))

    def test_traffic_reports_stale_refusals_and_sample_hits(self):
        body = TestClient(backend.app).get("/api/proxy/traffic", auth=("op", "pw")).json()
        self.assertIn("stale_refusals", body["totals"])
        self.assertIn("sample_hits", body["totals"])
