"""The packaged MCP server against a fake Aperture (no network, no LND)."""
import asyncio
import hashlib
import json
import os
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "auragrid_mcp"))

from auragrid_mcp import server as srv  # noqa: E402

PREIMAGE = "ef" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()
MANIFEST = {
    "status_url": "https://x/status.json", "terms_url": "https://x/terms", "plans": [],
    "endpoints": [
        {"path": "/api/v1/batch/btc-fees", "name": "BTC fees", "access": "paid", "status": "live",
         "price_sats": 8, "token_ttl_seconds": 3600, "sample_url": "/api/v1/batch/btc-fees-delayed"},
        {"path": "/api/v1/batch/btc-fees-delayed", "name": "BTC fees (delayed)", "access": "free", "status": "live"},
        {"path": "/api/v1/energy/grid", "name": "Grid", "access": "free", "status": "live"},
    ],
}
FEED = {"feed": "x", "stale": False, "provenance": {"as_of": "2026-11-19T12:00:00+00:00"}, "data": {"v": 1}}


class Resp:
    def __init__(self, status, body, headers=None):
        self.status_code, self.headers = status, headers or {}
        self.content = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.text, self.request = self.content.decode(), None

    def json(self):
        return json.loads(self.content)

    def iter_content(self, _n):
        yield self.content

    def close(self):
        pass


class FakeAperture:
    """Paid path: 402 without a token, 200 with the token we issued. Free paths: 200."""

    def __init__(self, paid_status=200):
        self.calls, self.paid_status = [], paid_status

    def get(self, url, params=None, headers=None, **_kw):
        path = url.split("https://x", 1)[1]
        headers = headers or {}
        self.calls.append((path, headers.get("Authorization")))
        if path == "/agent-manifest.json":
            return Resp(200, MANIFEST)
        if path.startswith("/api/v1/price/"):
            return Resp(200, {"price_sats": 8, "tolerance_pct": 3, "token_ttl_seconds": 3600,
                              "sample_url": "/api/v1/batch/btc-fees-delayed"})
        if path == "/api/v1/batch/btc-fees":
            if headers.get("Authorization") == "L402 mac:" + PREIMAGE:
                return Resp(self.paid_status, FEED if self.paid_status == 200 else {"stale": True})
            return Resp(402, b"payment required", {"WWW-Authenticate": 'L402 macaroon="mac", invoice="lnbc80n1x"'})
        if path in ("/api/v1/batch/btc-fees-delayed", "/api/v1/energy/grid"):
            return Resp(200, FEED)
        return Resp(404, {"detail": "not found"})


class FakeWallet:
    def __init__(self, amount=8):
        self.amount, self.paid = amount, []

    def decode(self, invoice):
        return {"amount_sats": self.amount, "payment_hash": PAYMENT_HASH, "expires_at": time.time() + 600}

    def pay(self, invoice, fee_limit_sats, timeout_seconds=60):
        self.paid.append(invoice)
        return PREIMAGE, 0


def cfg(env=None, wallet=None, aperture=None):
    env = {"AURAGRID_URL": "https://x", "AURAGRID_SIGNING_PUBKEY": "none", **(env or {})}
    return srv.Config(env=env, session=aperture or FakeAperture(), wallet=wallet)


class McpServerTests(unittest.TestCase):
    def test_tools_are_registered(self):
        names = {t.name for t in asyncio.run(srv.build_server(cfg()).list_tools())}
        self.assertEqual(names, {"list_feeds", "get_sample", "get_quote", "fetch"})

    def test_list_feeds_and_sample(self):
        c = cfg()
        self.assertEqual(len(srv.list_feeds(c)["feeds"]), 3)
        s = srv.get_sample(c, "btc-fees")
        self.assertEqual(s["path"], "/api/v1/batch/btc-fees-delayed")
        self.assertEqual(s["data"], {"v": 1})

    def test_quote(self):
        self.assertEqual(srv.get_quote(cfg(), "batch/btc-fees")["price_sats"], 8)
        self.assertEqual(srv.get_quote(cfg(), "energy/grid")["access"], "free")

    def test_paid_fetch_without_wallet_never_pays(self):
        ap = FakeAperture()
        out = srv.fetch(cfg(aperture=ap), "btc-fees")
        self.assertEqual(out["error"], "payment not configured")
        self.assertEqual(out["sample_url"], "/api/v1/batch/btc-fees-delayed")
        self.assertFalse(any(auth for _p, auth in ap.calls))

    def test_paid_fetch_needs_a_cap_even_with_a_wallet(self):
        wallet = FakeWallet()
        out = srv.fetch(cfg(wallet=wallet), "btc-fees")
        self.assertEqual(out["error"], "payment not configured")
        self.assertEqual(wallet.paid, [])

    def test_paid_fetch_within_cap_pays_once_and_reuses_the_token(self):
        wallet, ap = FakeWallet(), FakeAperture()
        c = cfg(env={"AURAGRID_MAX_SATS": "20"}, wallet=wallet, aperture=ap)
        self.assertEqual(srv.fetch(c, "btc-fees")["data"], {"v": 1})
        self.assertEqual(srv.fetch(c, "btc-fees")["data"], {"v": 1})
        self.assertEqual(len(wallet.paid), 1)

    def test_invoice_above_cap_is_refused(self):
        wallet = FakeWallet(amount=500)
        out = srv.fetch(cfg(env={"AURAGRID_MAX_SATS": "20"}, wallet=wallet), "btc-fees")
        self.assertEqual(out["error"], "PriceRejected")
        self.assertEqual(wallet.paid, [])

    def test_invoice_above_quote_is_refused(self):
        wallet = FakeWallet(amount=15)   # quote is 8 sats (+3%)
        out = srv.fetch(cfg(env={"AURAGRID_MAX_SATS": "100"}, wallet=wallet), "btc-fees")
        self.assertEqual(out["error"], "PriceRejected")
        self.assertEqual(wallet.paid, [])

    def test_unsigned_response_is_rejected_when_a_key_is_pinned(self):
        c = cfg(env={"AURAGRID_SIGNING_PUBKEY": srv.DEFAULT_SIGNING_PUBKEY})
        out = srv.get_sample(c, "btc-fees")
        self.assertEqual(out["error"], "signature verification failed")
        self.assertNotIn("data", out)

    def test_bad_feed_names_are_errors_not_requests(self):
        ap = FakeAperture()
        server = srv.build_server(cfg(aperture=ap))
        for bad in ("../admin", "https://evil/x", "nope"):
            out = srv._safe(srv.fetch, cfg(aperture=ap), bad)
            self.assertIn("error", out, bad)
        self.assertTrue(all(p == "/agent-manifest.json" for p, _ in ap.calls))
        self.assertIsNotNone(server)


class PackagedCopies(unittest.TestCase):
    def test_verification_code_matches_attestation_py(self):
        with open(os.path.join(ROOT, "attestation.py")) as fh:
            root = fh.read()
        root_verify = root[root.index("import asyncio"):root.index("# Server side: v2 signing")]
        with open(os.path.join(ROOT, "auragrid_mcp", "auragrid_mcp", "attestation_verify.py")) as fh:
            packaged = fh.read()
        packaged = packaged[packaged.index("import asyncio"):]
        self.assertEqual(packaged.strip(), root_verify.rstrip().rstrip("#-\n ").strip())
