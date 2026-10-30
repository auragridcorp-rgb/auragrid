"""Offline tests for passes/bundles, samples, gateway checks, ledger plans, attestation skips.

    python -m unittest discover -s tests -v

No LND, Aperture or network needed: LND is faked, the BTC feed never starts
(the lifespan is not run), and feed providers are stubs registered here only.
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
    "AURAGRID_SAMPLES_DIR": os.path.join(_TMP, "samples"),
    "AURAGRID_SAMPLE_DELAY": "0",
    "AURAGRID_ADMIN_USER": "op",
    "AURAGRID_ADMIN_PASSWORD": "pw",
})
os.environ.pop("AURAGRID_ATTEST_MACAROON", None)
os.environ.pop("ERCOT_SUBSCRIPTION_KEY", None)
os.environ.pop("EIA_API_KEY", None)

import feeds  # noqa: E402

STUB = {"nodes": list(range(50)), "region": "test"}
for _key in ("ercot-lmp", "lng-supply-chain", "port-congestion-index"):
    feeds.provider(_key)(lambda: STUB)

from fastapi.testclient import TestClient  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

import app.main as backend  # noqa: E402
import gen_aperture  # noqa: E402
import samples  # noqa: E402
from attestation import AttestationMiddleware  # noqa: E402
from l402_ledger import Ledger  # noqa: E402
from pass_support import parse_plans  # noqa: E402

GW = {"X-AurAgrid-Gateway": SECRET}
PREIMAGE = "ab" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()
L402 = {"Authorization": "L402 AgEEbHNhdA:" + PREIMAGE}


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
        backend.sample_store.snapshot_once()

    # -- paywall / gateway ------------------------------------------------------
    def test_paid_route_requires_gateway_secret(self):
        self.assertEqual(self.client.get("/api/v1/batch/ercot-lmp").status_code, 403)
        self.assertEqual(self.client.get("/api/v1/batch/ercot-lmp", headers=L402).status_code, 403)
        bad = {"X-AurAgrid-Gateway": "x" * 40}
        self.assertEqual(self.client.get("/api/v1/batch/ercot-lmp", headers=bad).status_code, 403)
        r = self.client.get("/api/v1/batch/ercot-lmp", headers=GW)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan"], "hourly")
        self.assertEqual(len(r.json()["data"]["nodes"]), 50)   # paid = full data

    def test_unconnected_feed_is_503_not_fake_data(self):
        r = self.client.get("/api/v1/batch/agri-yield", headers=GW)
        self.assertEqual(r.status_code, 503)

    def test_pass_and_bundle_rewrite(self):
        r = self.client.get("/api/v1/pro/ercot-lmp", headers=GW)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan"], "pass_30d")
        self.assertEqual(r.json()["feed"], "ercot-lmp")
        r = self.client.get("/api/v1/bundle/port-congestion-index", headers=GW)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["plan"], "bundle")

    def test_plan_paths_cannot_reach_other_feeds(self):
        self.assertEqual(self.client.get("/api/v1/pro/lng-supply-chain", headers=GW).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/bundle/esg/carbon", headers=GW).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/bundle/naval-escalation", headers=GW).status_code, 404)
        self.assertEqual(self.client.get("/api/v1/pro/ercot-lmp").status_code, 403)

    # -- samples ----------------------------------------------------------------
    def test_sample_is_free_delayed_and_truncated(self):
        r = self.client.get("/api/v1/sample/lng-supply")          # alias
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["mode"], "sample_delayed")
        self.assertEqual(body["feed"], "lng-supply-chain")
        self.assertEqual(len(body["data"]["nodes"]), samples.MAX_ITEMS)
        self.assertEqual(body["truncated"], {"$.nodes": 50})
        self.assertIn("as_of", body)
        self.assertEqual(self.client.get("/api/v1/sample/naval-escalation").status_code, 404)

    def test_sample_warming_up_is_503(self):
        store = samples.SampleStore(directory=os.path.join(_TMP, "warm"), delay=3600)
        store.snapshot_once()
        self.assertIsNone(store.get("ercot-lmp"))
        old, backend.sample_store = backend.sample_store, store
        try:
            r = self.client.get("/api/v1/sample/ercot-lmp")
            self.assertEqual(r.status_code, 503)
            self.assertIn("Retry-After", r.headers)
        finally:
            backend.sample_store = old

    def test_sample_survives_restart(self):
        reloaded = samples.SampleStore(directory=os.environ["AURAGRID_SAMPLES_DIR"], delay=0)
        self.assertIsNotNone(reloaded.get("port-congestion-index"))

    # -- quotes / discovery -----------------------------------------------------
    def test_plan_quotes(self):
        pro = self.client.get("/api/v1/price/pro/ercot-lmp").json()
        self.assertGreater(pro["price_sats"], 0)
        self.assertEqual(pro["plan"], "pass_30d")
        self.assertEqual(pro["token_ttl_seconds"], 2592000)
        bundle = self.client.get("/api/v1/price/bundle/ercot-lmp").json()
        self.assertEqual(bundle["pricing"], "static")
        self.assertEqual(bundle["plan"], "bundle")

    def test_manifest_lists_plans_and_samples(self):
        m = self.client.get("/agent-manifest.json").json()
        self.assertEqual({p["name"] for p in m["plans"]}, {"ercot-pro-pass", "hero-bundle"})
        self.assertEqual(len(m["samples"]), 3)
        self.assertEqual(self.client.get("/llms.txt").status_code, 200)
        self.assertIn("/api/v1/batch/ercot-lmp", self.client.get("/openapi.json").json()["paths"])

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
        r = self.client.get("/api/v1/pro/ercot-lmp", headers={**GW, **L402})
        self.assertEqual(r.status_code, 200)
        self.client.get("/api/v1/sample/ercot-lmp", headers=L402)   # free: must not be recorded
        backend.ledger.flush()
        self.assertEqual(backend.ledger.plans_by_hash().get(PAYMENT_HASH), "pass_30d")
        self.assertEqual(backend.ledger.endpoints_by_hash().get(PAYMENT_HASH), "pro/ercot-lmp")

        invoices = [
            {"r_hash": PAYMENT_HASH, "value": "63100", "amt_paid_sat": "63100", "state": "SETTLED", "creation_date": "2"},
            {"r_hash": "cd" * 32, "value": "127000", "amt_paid_sat": "127000", "state": "SETTLED", "creation_date": "1"},
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
        if backend.PLAN_PRICES.get("hero-bundle") == 127000:   # price fallback for un-ledgered invoices
            self.assertEqual(plans["bundle"]["revenue_sats"], 127000)
        self.assertEqual(stats["total_revenue_sats"], 63100 + 127000)
        self.assertEqual(txs[0]["plan"], "pass_30d")

    def test_ledger_migrates_old_schema(self):
        path = os.path.join(_TMP, "old.db")
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE token_usage (payment_hash TEXT PRIMARY KEY, endpoint TEXT NOT NULL, "
                     "first_seen INTEGER NOT NULL, last_seen INTEGER NOT NULL, calls INTEGER NOT NULL DEFAULT 0)")
        conn.execute("INSERT INTO token_usage VALUES ('aa', 'ercot-lmp', 1, 1, 3)")
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
        self.assertNotIn("x-auragrid-signature-v2", client.get("/api/v1/sample/ercot-lmp").headers)
        self.assertNotIn("x-auragrid-signature-v2", client.get("/api/v1/price/pro/ercot-lmp").headers)
        self.assertEqual(calls, [])
        self.assertEqual(client.get("/api/v1/pro/ercot-lmp").headers["x-auragrid-signature-v2"], "sig")
        self.assertEqual(len(calls), 1)
        self.assertIn("/api/v1/pro/ercot-lmp", calls[0])


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        import yaml
        with open("aperture.yaml") as fh:
            self.base = yaml.safe_load(fh)
        self.plans = backend.PLANS

    def test_generated_config_is_valid(self):
        config = gen_aperture.build(self.base, self.plans, 77000.0, "s" * 40)
        self.assertEqual(gen_aperture.validate(config, self.plans), [])
        names = [s["name"] for s in config["services"]]
        self.assertEqual(names[-1], "public-free")
        pro = next(s for s in config["services"] if s["name"] == "ercot-pro-pass")
        self.assertEqual(pro["timeout"], 2592000)
        self.assertEqual(pro["pathregexp"], "^/api/v1/pro/ercot-lmp$")
        self.assertTrue(pro["dynamicprice"]["enabled"])
        bundle = next(s for s in config["services"] if s["name"] == "hero-bundle")
        self.assertNotIn("dynamicprice", bundle)   # dynamic tokens are per-path; a bundle must be static
        self.assertTrue(all(s["headers"]["X-AurAgrid-Gateway"] == "s" * 40
                            for s in config["services"] if s["name"] != "public-free"))
        self.assertNotIn("headers", config["services"][-1])
        self.assertIn("lndhost", config["authenticator"])
        self.assertNotIn("lndhost", config)

    def test_public_rule_exposes_only_free_paths(self):
        import re
        rx = re.compile(gen_aperture.PUBLIC_PATHS)
        for path in ("/api/v1/sample/ercot-lmp", "/api/v1/price/pro/ercot-lmp", "/buy", "/llms.txt",
                     "/agent-manifest.json", "/.well-known/ai-plugin.json", "/openapi.json"):
            self.assertTrue(rx.match(path), path)
        for path in ("/", "/api/proxy/stats", "/api/v1/batch/ercot-lmp", "/api/v1/pro/ercot-lmp",
                     "/api/v1/sample/../batch/ercot-lmp", "/docs", "/buy/../api/proxy/stats"):
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
