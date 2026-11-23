"""x402 rail (x402_rail.py): config, 402 challenge, stale gate before payment, settle-after-2xx, ledger."""
import base64
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
import gen_aperture  # noqa: E402
import x402_rail  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from l402_ledger import Ledger  # noqa: E402
from x402.schemas import SettleResponse, SupportedKind, SupportedResponse, VerifyResponse  # noqa: E402

PAY_TO = "0x" + "a" * 40
PAYER = "0x" + "b" * 40
TX = "0x" + "c" * 64
BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class FakeFacilitator:
    def __init__(self, valid=True):
        self.valid, self.calls = valid, []

    def get_supported(self):
        return SupportedResponse(kinds=[SupportedKind(x402_version=2, scheme="exact", network="eip155:8453")])

    async def verify(self, payload, requirements):
        self.calls.append("verify")
        return VerifyResponse(is_valid=self.valid, payer=PAYER, invalid_reason=None if self.valid else "bad")

    async def settle(self, payload, requirements):
        self.calls.append("settle")
        return SettleResponse(success=True, payer=PAYER, transaction=TX, network="eip155:8453")


def payment_header(required: dict) -> str:
    acc = required["accepts"][0]
    payload = {"x402Version": 2, "accepted": acc, "resource": required.get("resource"),
               "payload": {"signature": "0x" + "d" * 130,
                           "authorization": {"from": PAYER, "to": acc["payTo"], "value": acc["amount"],
                                             "validAfter": "0", "validBefore": "9999999999",
                                             "nonce": "0x" + "e" * 64}}}
    return base64.b64encode(json.dumps(payload).encode()).decode()


class Rail:
    """A tiny app with the rail installed; `state` drives staleness and the handler."""

    def __init__(self, cfg="default", valid=True):
        self.stale, self.fail = set(), False
        self.fac = FakeFacilitator(valid)
        self.ledger = Ledger(os.path.join(tempfile.mkdtemp(), "ledger.db"))
        self.cfg = x402_rail.Config(pay_to=PAY_TO, facilitator_url="https://fac.example",
                                    prices={"btc-fees": 0.002, "ln-network": 0.004}) if cfg == "default" else cfg
        self.app = FastAPI()
        from fastapi.responses import JSONResponse

        def handler_for(key):
            def handler():
                if self.fail:
                    raise HTTPException(status_code=503, detail="went stale")
                return {"feed": key, "data": {"x": 1}}
            return handler

        x402_rail.install(self.app, self.cfg, describe=lambda k: {"name": k}, degraded=lambda k: k in self.stale,
                          stale_refusal=lambda k: JSONResponse({"stale": True, "feed": k}, status_code=503),
                          ledger=self.ledger, handler_for=handler_for, facilitator=self.fac)
        self.client = TestClient(self.app)

    def challenge(self, path="/x402/v1/batch/btc-fees"):
        r = self.client.get(path)
        return r, (json.loads(base64.b64decode(r.headers["payment-required"])) if "payment-required" in r.headers
                   else None)


class ConfigTests(unittest.TestCase):
    PRICES = {"btc-fees": 0.002}

    def test_off_unless_configured(self):
        self.assertIsNone(x402_rail.load_config(self.PRICES, env={}))

    def test_rejects_bad_settings(self):
        bad = [
            {"AURAGRID_X402_PAY_TO": "0x123", "AURAGRID_X402_FACILITATOR_URL": "https://f.example"},
            {"AURAGRID_X402_PAY_TO": PAY_TO, "AURAGRID_X402_FACILITATOR_URL": "http://f.example"},
            # x402.org's free facilitator only settles testnet payments
            {"AURAGRID_X402_PAY_TO": PAY_TO, "AURAGRID_X402_FACILITATOR_URL": "https://x402.org/facilitator"},
        ]
        for env in bad:
            self.assertIsNone(x402_rail.load_config(self.PRICES, env=env), env)
        ok = {"AURAGRID_X402_PAY_TO": PAY_TO, "AURAGRID_X402_FACILITATOR_URL": "https://f.example/"}
        self.assertIsNone(x402_rail.load_config({"btc-fees": 0}, env=ok))      # no positive price
        cfg = x402_rail.load_config(self.PRICES, env=ok)
        self.assertEqual((cfg.network, cfg.facilitator_url, cfg.price_str("btc-fees")),
                         ("eip155:8453", "https://f.example", "$0.002"))
        test = x402_rail.load_config(self.PRICES, env={**ok, "AURAGRID_X402_FACILITATOR_URL": "https://x402.org/facilitator",
                                                       "AURAGRID_X402_NETWORK": "eip155:84532"})
        self.assertIsNotNone(test)

    def test_cdp_key_file(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cdp.json")
        with open(path, "w") as fh:
            json.dump({"id": "key-id", "privateKey": "secret"}, fh)
        os.chmod(path, 0o644)
        env = {"AURAGRID_X402_PAY_TO": PAY_TO, "AURAGRID_X402_CDP_KEY_FILE": path}
        self.assertIsNone(x402_rail.load_config(self.PRICES, env=env))      # readable by others: refused
        os.chmod(path, 0o600)
        cfg = x402_rail.load_config(self.PRICES, env=env)
        self.assertEqual(cfg.facilitator_url, x402_rail.CDP_FACILITATOR_URL)
        self.assertEqual(x402_rail.cdp_credentials(path), ("key-id", "secret"))
        with open(path, "w") as fh:
            json.dump({"name": "organizations/x/apiKeys/y"}, fh)           # no secret
        self.assertIsNone(x402_rail.load_config(self.PRICES, env=env))
        self.assertIsNone(x402_rail.load_config(self.PRICES, env={**env, "AURAGRID_X402_CDP_KEY_FILE": d + "/nope"}))

    def test_cdp_facilitator_is_built_from_the_key_file(self):
        d = tempfile.mkdtemp()
        path = os.path.join(d, "cdp.json")
        with open(path, "w") as fh:
            json.dump({"name": "organizations/o/apiKeys/k",
                       "privateKey": "-----BEGIN EC PRIVATE KEY-----\nx\n-----END EC PRIVATE KEY-----"}, fh)
        os.chmod(path, 0o600)
        cfg = x402_rail.Config(pay_to=PAY_TO, facilitator_url=x402_rail.CDP_FACILITATOR_URL,
                               prices={"btc-fees": 0.002}, cdp_key_file=path)
        import x402.http as xh
        seen = {}
        real = xh.HTTPFacilitatorClient

        class Spy(real):
            def __init__(self, config=None, *a, **k):
                seen["url"] = config.get("url") if isinstance(config, dict) else getattr(config, "url", None)
                seen["auth"] = bool(config.get("create_headers")) if isinstance(config, dict) else None
                raise RuntimeError("stop before any network call")
        xh.HTTPFacilitatorClient = Spy
        try:
            with self.assertRaises(RuntimeError):
                x402_rail.build_middleware(cfg, lambda k: {})
        finally:
            xh.HTTPFacilitatorClient = real
        self.assertEqual(seen, {"url": x402_rail.CDP_FACILITATOR_URL, "auth": True})

    def test_feed_key(self):
        self.assertEqual(x402_rail.feed_key("/x402/v1/batch/btc-fees"), "btc-fees")
        self.assertIsNone(x402_rail.feed_key("/x402/v1/batch/../admin"))
        self.assertIsNone(x402_rail.feed_key("/api/v1/batch/btc-fees"))


class FlowTests(unittest.TestCase):
    def test_disabled_rail_says_so(self):
        r = Rail(cfg=None).client.get("/x402/v1/batch/btc-fees")
        self.assertEqual(r.status_code, 404)
        self.assertIn("not enabled", r.json()["detail"])

    def test_unpaid_request_gets_a_402_with_the_exact_price(self):
        rail = Rail()
        r, req = rail.challenge()
        self.assertEqual(r.status_code, 402)
        acc = req["accepts"][0]
        self.assertEqual((acc["scheme"], acc["network"], acc["payTo"], acc["amount"], acc["asset"]),
                         ("exact", "eip155:8453", PAY_TO, "2000", BASE_USDC))   # $0.002 = 2000 micro-USDC
        self.assertEqual(rail.challenge("/x402/v1/batch/ln-network")[1]["accepts"][0]["amount"], "4000")
        self.assertEqual(rail.fac.calls, [])

    def test_stale_feed_asks_for_no_payment(self):
        rail = Rail()
        rail.stale.add("btc-fees")
        r = rail.client.get("/x402/v1/batch/btc-fees")
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("payment-required", r.headers)
        self.assertEqual(rail.fac.calls, [])

    def test_paid_request_is_served_settled_and_recorded_once(self):
        rail = Rail()
        hdr = payment_header(rail.challenge()[1])
        r = rail.client.get("/x402/v1/batch/btc-fees", headers={"PAYMENT-SIGNATURE": hdr})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["feed"], "btc-fees")
        self.assertEqual(rail.fac.calls, ["verify", "settle"])
        self.assertEqual(x402_rail.decode_payment_response(r.headers["payment-response"])["transaction"], TX)
        rail.ledger.record_x402(TX, "btc-fees", "eip155:8453", PAYER, 0.002)    # replay of the same receipt
        s = rail.ledger.x402_summary()
        self.assertEqual((s["payments"], s["usd"], s["distinct_payers"]), (1, 0.002, 1))
        self.assertEqual(s["by_endpoint"]["btc-fees"]["payments"], 1)

    def test_failed_handler_is_not_settled(self):
        rail = Rail()
        hdr = payment_header(rail.challenge()[1])
        rail.fail = True
        r = rail.client.get("/x402/v1/batch/btc-fees", headers={"PAYMENT-SIGNATURE": hdr})
        self.assertEqual(r.status_code, 503)
        self.assertNotIn("settle", rail.fac.calls)
        self.assertEqual(rail.ledger.x402_summary()["payments"], 0)

    def test_invalid_payment_is_refused(self):
        rail = Rail(valid=False)
        hdr = payment_header(rail.challenge()[1])
        r = rail.client.get("/x402/v1/batch/btc-fees", headers={"PAYMENT-SIGNATURE": hdr})
        self.assertEqual(r.status_code, 402)
        self.assertNotIn("settle", rail.fac.calls)

    def test_unknown_x402_path(self):
        r = Rail().client.get("/x402/v1/batch/energy-grid")
        self.assertEqual(r.status_code, 404)
        self.assertIn("/x402/v1/batch/btc-fees", r.json()["sold"])


class WiringTests(unittest.TestCase):
    def test_production_app_answers_x402_paths_when_off(self):
        r = TestClient(t.backend.app).get("/x402/v1/batch/btc-fees")
        self.assertEqual(r.status_code, 404)       # the test environment sets no pay-to address
        self.assertIn("not enabled", r.json()["detail"])

    def test_dashboard_counts_x402_as_paid_and_aperture_passes_it_through(self):
        self.assertEqual(t.backend.classify_path("/x402/v1/batch/btc-fees"), "paid")
        self.assertEqual(t.backend.classify_path("/x402/v1/batch/nope"), "noise")
        self.assertTrue(re.match(gen_aperture.PUBLIC_PATHS, "/x402/v1/batch/btc-fees"))
        self.assertFalse(re.match(gen_aperture.PUBLIC_PATHS, "/x402/v1/../api/proxy/stats"))

    def test_discovery_mentions_x402_only_when_on(self):
        b = t.backend
        client = TestClient(b.app)
        self.assertNotIn("x402", client.get("/agent-manifest.json").json())
        saved = b.X402
        b.X402 = x402_rail.Config(pay_to=PAY_TO, facilitator_url="https://f.example", prices={"btc-fees": 0.002})
        try:
            m = client.get("/agent-manifest.json").json()
            self.assertEqual(m["payment_protocols"], ["L402", "x402"])
            self.assertEqual(m["x402"]["endpoints"][0]["price_usd"], 0.002)
            self.assertTrue(m["x402"]["endpoints"][0]["url"].endswith("/x402/v1/batch/btc-fees"))
            self.assertIn("x402", client.get("/llms.txt").text)
        finally:
            b.X402 = saved

    def test_x402_prices_come_from_pricing_yaml_paid_feeds_only(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prices = t.backend._x402_prices(os.path.join(here, "pricing.yaml"))
        self.assertEqual(set(prices), {"btc-fees", "ln-network"} & set(t.backend.ACCESS.paid))
        self.assertTrue(all(0 < v < 0.05 for v in prices.values()))


if __name__ == "__main__":
    unittest.main()
