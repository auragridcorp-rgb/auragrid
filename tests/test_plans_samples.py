"""Offline tests for paid/free routing, passes/bundles, gateway checks, retired endpoints,
the ledger's plan split and attestation skips.

    python -m unittest discover -s tests -t .

No LND, Aperture or network needed: LND is faked, the BTC price feed never starts
(the lifespan is not run), and feed data is written straight into a temporary
snapshot store, the same way fetcher.py does.
"""
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

_TMP = tempfile.mkdtemp(prefix="auragrid-test-")
SECRET = "g" * 40
os.environ.update({
    "AURAGRID_GATEWAY_SECRET": SECRET,
    "AURAGRID_LEDGER_DB": os.path.join(_TMP, "ledger.db"),
    "AURAGRID_FEEDS_DB": os.path.join(_TMP, "feeds.db"),
    "AURAGRID_ADMIN_USER": "op",
    "AURAGRID_ADMIN_PASSWORD": "pw",
    "AURAGRID_NDBC_ENABLED": "0",   # no network in tests
    # frozen copies, so the tests do not depend on what is currently live
    "AURAGRID_PRICING_CONFIG": os.path.join(ROOT, "tests", "fixtures", "pricing.yaml"),
    "AURAGRID_APERTURE_CONFIG": os.path.join(ROOT, "tests", "fixtures", "aperture.yaml"),
})
for _var in ("AURAGRID_ATTEST_MACAROON", "ERCOT_SUBSCRIPTION_KEY", "EIA_API_KEY"):
    os.environ.pop(_var, None)

from fastapi.testclient import TestClient  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import app.main as backend  # noqa: E402
import gen_aperture  # noqa: E402
from attestation import AttestationMiddleware  # noqa: E402
from l402_ledger import Ledger  # noqa: E402
from pass_support import parse_plans  # noqa: E402

GW = {"X-AurAgrid-Gateway": SECRET}
PREIMAGE = "ab" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()
L402 = {"Authorization": "L402 AgEEbHNhdA:" + PREIMAGE}

BTC = {"tip": {"height": 1, "block_time": 0, "block_time_utc": "2026-10-30T12:00:00+00:00"},
       "fee_estimates": {"1": {"sat_per_vb": 2.0, "blocks": 2}}, "rows": list(range(50))}
LN = {"observed_at": "2026-10-30T12:00:00+00:00", "nodes": 10, "channels": 20}


def seed_store():
    backend.STORE.save("btc-fees", BTC, BTC["tip"]["block_time_utc"])
    backend.STORE.save("ln-network", LN, LN["observed_at"])


class FakeLND:
    def __init__(self, invoices):
        self.invoices = invoices

    def get_invoices(self):
        return self.invoices

    def get_mainnet_node_stats(self):
        return {"node_alias": "test", "network": "mainnet", "active_channels": 1, "total_capacity": 1}


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(backend.app)
        seed_store()

    # -- paywall / gateway ------------------------------------------------------
    def test_paid_route_requires_gateway_secret(self):
        self.assertEqual(self.client.get("/api/v1/batch/btc-fees").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/batch/btc-fees", headers=L402).status_code, 403)
        bad = {"X-AurAgrid-Gateway": "x" * 40}
        self.assertEqual(self.client.get("/api/v1/batch/btc-fees", headers=bad).status_code, 403)
        r = self.client.get("/api/v1/batch/btc-fees", headers=GW)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["plan"], "hourly")
        self.assertEqual(len(body["data"]["rows"]), 50)                         # paid = full data
        self.assertEqual(body["provenance"]["as_of"], "2026-10-30T12:00:00+00:00")
        self.assertIn("Bitcoin Core", body["provenance"]["source"])

    def test_feed_without_snapshot_is_503_not_fake_data(self):
        r = self.client.get("/api/v1/batch/earthquakes")                       # free, nothing stored
        self.assertEqual(r.status_code, 503)

    def test_pass_and_bundle_rewrite(self):
        r = self.client.get("/api/v1/pass30/btc-fees", headers=GW)
        self.assertEqual(r.status_code, 200)
        self.assertEqual((r.json()["plan"], r.json()["feed"]), ("pass_30d", "btc-fees"))
        r = self.client.get("/api/v1/bundle/ln-network", headers=GW)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan"], "bundle")

    def test_plan_paths_cannot_reach_other_feeds(self):
        self.assertEqual(self.client.get("/api/v1/pass30/ln-network", headers=GW).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/bundle/earthquakes", headers=GW).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/bundle/esg/carbon", headers=GW).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/pass30/btc-fees").status_code, 403)

    # -- retired endpoints ------------------------------------------------------
    def test_retired_endpoints_are_gone(self):
        for path in ("/api/v1/batch/ercot-lmp", "/api/v1/esg/carbon", "/api/v1/sample/ercot-lmp",
                     "/api/v1/pro/ercot-lmp", "/api/v1/sample/lng-supply"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 410, path)
            self.assertIn("retired", r.json()["detail"])

    # -- quotes / discovery -----------------------------------------------------
    def test_plan_quotes(self):
        pass30 = self.client.get("/api/v1/price/pass30/btc-fees").json()
        self.assertGreater(pass30["price_sats"], 0)
        self.assertEqual((pass30["plan"], pass30["token_ttl_seconds"]), ("pass_30d", 2592000))
        bundle = self.client.get("/api/v1/price/bundle/btc-fees").json()
        self.assertEqual((bundle["pricing"], bundle["plan"], bundle["price_sats"]), ("static", "bundle", 5000))
        hourly = self.client.get("/api/v1/price/batch/btc-fees").json()
        self.assertEqual((hourly["price_sats"], hourly["token_ttl_seconds"]), (50, 3600))

    def test_manifest_lists_plans_and_feeds(self):
        m = self.client.get("/agent-manifest.json").json()
        self.assertEqual({p["name"] for p in m["plans"]}, {"fees-pass", "lightning-operator"})
        self.assertNotIn("samples", m)
        paths = {e["path"] for e in m["endpoints"]}
        self.assertIn("/api/v1/batch/btc-fees-delayed", paths)
        self.assertNotIn("/api/v1/batch/ercot-lmp", paths)
        self.assertEqual(self.client.get("/llms.txt").status_code, 200)
        self.assertIn("/api/v1/batch/btc-fees", self.client.get("/openapi.json").json()["paths"])

    def test_buy_page_has_strict_csp(self):
        r = self.client.get("/buy")
        self.assertEqual(r.status_code, 200)
        csp = r.headers["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn('nonce="%s"' % nonce, r.text)
        self.assertNotIn("__NONCE__", r.text)
        self.assertNotIn("innerHTML", r.text)

    def test_operator_routes_locked(self):
        for path in ("/", "/api/proxy/stats", "/api/proxy/transactions", "/api/proxy/info"):
            self.assertEqual(self.client.get(path).status_code, 401, path)

    # -- ledger / revenue by plan --------------------------------------------------
    def test_revenue_split_by_plan(self):
        r = self.client.get("/api/v1/pass30/btc-fees", headers={**GW, **L402})
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/v1/price/batch/btc-fees", headers=L402)   # free quote: must not be recorded
        backend.ledger.flush()
        self.assertEqual(backend.ledger.plans_by_hash().get(PAYMENT_HASH), "pass_30d")
        self.assertEqual(backend.ledger.endpoints_by_hash().get(PAYMENT_HASH), "pass30/btc-fees")

        invoices = [
            {"r_hash": PAYMENT_HASH, "value": "63100", "amt_paid_sat": "63100", "state": "SETTLED", "creation_date": "2"},
            {"r_hash": "cd" * 32, "value": "5000", "amt_paid_sat": "5000", "state": "SETTLED", "creation_date": "1"},
            {"r_hash": "ef" * 32, "value": "5", "state": "CANCELED", "creation_date": "0"},
        ]
        old = backend.lnd
        backend.lnd, backend._cache["t"] = FakeLND(invoices), 0.0
        try:
            stats = self.client.get("/api/proxy/stats", auth=("op", "pw")).json()
            txs = self.client.get("/api/proxy/transactions", auth=("op", "pw")).json()["transactions"]
        finally:
            backend.lnd, backend._cache["t"] = old, 0.0
        plans = stats["revenue_by_plan"]
        self.assertEqual(plans["pass_30d"]["revenue_sats"], 63100)
        self.assertEqual(plans["bundle"]["revenue_sats"], 5000)        # static bundle price identifies it
        self.assertEqual(stats["total_revenue_sats"], 63100 + 5000)
        self.assertEqual(txs[0]["plan"], "pass_30d")

    def test_ledger_migrates_old_schema(self):
        path = os.path.join(_TMP, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE token_usage (payment_hash TEXT PRIMARY KEY, endpoint TEXT NOT NULL, "
                     "first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO token_usage VALUES ('aa', 'btc-fees', 1, 1, 3)")
        conn.commit()
        conn.close()
        led = Ledger(path)
        self.assertEqual(led.plans_by_hash(), {"aa": "hourly"})
        led.stop()


class AttestationSkipTests(unittest.TestCase):
    def test_free_paths_are_not_signed(self):
        calls = []

        def sign(message):
            calls.append(message)
            return "sig"

        async def ok(request):
            return JSONResponse({"ok": True})

        inner = Starlette(routes=[Route("/api/v1/{rest:path}", ok)])
        client = TestClient(AttestationMiddleware(inner, sign=sign))
        self.assertNotIn("x-auragrid-signature-v2", client.get("/api/v1/sample/btc-fees").headers)
        self.assertNotIn("x-auragrid-signature-v2", client.get("/api/v1/price/batch/btc-fees").headers)
        self.assertEqual(calls, [])
        self.assertEqual(client.get("/api/v1/batch/btc-fees").headers["x-auragrid-signature-v2"], "sig")
        self.assertEqual(len(calls), 1)
        self.assertIn("/api/v1/batch/btc-fees", calls[0])


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        import yaml
        from pass_support import load_access
        with open(os.path.join(ROOT, "aperture.base.yaml")) as fh:
            self.base = yaml.safe_load(fh)
        self.plans = backend.PLANS
        self.access = load_access(os.environ["AURAGRID_PRICING_CONFIG"])

    def test_generated_config_is_valid(self):
        config = gen_aperture.build(self.base, self.plans, 77000.0, "s" * 40, self.access)
        self.assertEqual(gen_aperture.validate(config, self.plans, self.access), [])
        names = [s["name"] for s in config["services"]]
        self.assertEqual(names[:2], ["free-feeds", "retired"])
        self.assertEqual(names[-1], "public-free")
        p30 = next(s for s in config["services"] if s["name"] == "fees-pass")
        self.assertEqual((p30["timeout"], p30["pathregexp"]), (2592000, "^/api/v1/pass30/btc-fees$"))
        self.assertTrue(p30["dynamicprice"]["enabled"])
        bundle = next(s for s in config["services"] if s["name"] == "lightning-operator")
        self.assertNotIn("dynamicprice", bundle)   # dynamic tokens are per-path; a bundle must be static
        self.assertEqual(bundle["price"], 5000)
        unpaid = {"public-free", "free-feeds", "retired"}
        self.assertTrue(all(s["headers"]["X-AurAgrid-Gateway"] == "s" * 40
                            for s in config["services"] if s["name"] not in unpaid))
        self.assertTrue(all("headers" not in s for s in config["services"] if s["name"] in unpaid))
        self.assertIn("lndhost", config["authenticator"])
        self.assertNotIn("lndhost", config)

    def test_paid_rules_never_catch_free_copies(self):
        import re
        config = gen_aperture.build(self.base, self.plans, 77000.0, None, self.access)
        first = lambda path: next(s["name"] for s in config["services"]           # noqa: E731
                                  if re.match(s["pathregexp"], path))
        self.assertEqual(first("/api/v1/batch/btc-fees-delayed"), "free-feeds")
        self.assertEqual(first("/api/v1/batch/ln-network-daily"), "free-feeds")
        self.assertEqual(first("/api/v1/batch/btc-fees"), "batch-btc-fees")
        self.assertEqual(first("/api/v1/batch/ercot-lmp"), "retired")
        self.assertEqual(first("/api/v1/sample/anything"), "retired")

    def test_public_rule_exposes_only_free_paths(self):
        import re
        rx = re.compile(gen_aperture.PUBLIC_PATHS)
        for path in ("/api/v1/price/batch/btc-fees", "/buy", "/llms.txt", "/terms",
                     "/agent-manifest.json", "/.well-known/ai-plugin.json", "/openapi.json"):
            self.assertTrue(rx.match(path), path)
        for path in ("/", "/api/proxy/stats", "/api/v1/batch/btc-fees", "/api/v1/pass30/btc-fees",
                     "/api/v1/sample/../batch/btc-fees", "/docs", "/buy/../api/proxy/stats"):
            self.assertFalse(rx.match(path), path)

    def test_bad_plans_rejected(self):
        plans = parse_plans({
            "a": {"kind": "pass_30d", "prefix": "pro", "feeds": ["x"], "usd": 1, "timeout": 10},
            "b": {"kind": "bundle", "prefix": "pro", "feeds": ["y"], "usd": 1, "timeout": 10},
            "c": {"kind": "pass_30d", "prefix": "batch", "feeds": ["x"], "usd": 1, "timeout": 10},
            "d": {"kind": "pass_30d", "prefix": "ok", "feeds": ["x"], "usd": 1, "timeout": "2592000s"},
        })
        self.assertEqual([p.name for p in plans], ["a"])


if __name__ == "__main__":
    unittest.main()
