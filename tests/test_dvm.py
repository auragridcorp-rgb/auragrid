"""Offline tests for the NIP-90 DVM: who it answers, and what it answers."""
import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["AURAGRID_DVM_KEY_FILE"] = os.path.join(tempfile.mkdtemp(), "dvm_key.txt")

from tests import test_plans_samples  # noqa: E402,F401  (shared test env must load before samples)
from dvm import nip90_worker as dvm  # noqa: E402

MANIFEST = {"endpoints": [
    {"path": "/api/v1/batch/maritime-weather", "name": "Marine Weather Observations", "access": "free", "status": "live"},
    {"path": "/api/v1/energy/grid", "name": "U.S. Grid", "access": "free", "status": "coming_soon"},
    {"path": "/api/v1/batch/ercot-lmp", "name": "ERCOT LMP", "access": "paid", "status": "live"},
]}


def job(kind=5990, tags=(), pubkey="c" * 64, jid="a" * 64):
    return {"id": jid, "pubkey": pubkey, "kind": kind, "tags": [list(t) for t in tags], "content": ""}


class FakeResp:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeHTTP:
    def __init__(self):
        self.urls = []

    async def get(self, url):
        self.urls.append(url)
        if url.endswith("/agent-manifest.json"):
            return FakeResp(MANIFEST)
        if "/api/v1/price/" in url:
            return FakeResp({"price_sats": 1234})
        return FakeResp({"feed": "maritime-weather", "served_at": "now",
                         "data": {"observations": [{"i": i} for i in range(500)], "stations": 500}})


class DVMTests(unittest.TestCase):
    def setUp(self):
        self.bot = dvm.DVM(dvm.load_key())
        self.bot.http = FakeHTTP()

    def run_reply(self, event):
        return asyncio.run(self.bot.reply(event))

    def test_ignores_jobs_not_for_us(self):
        self.assertIsNone(self.run_reply(job(kind=5002, tags=[("i", "translate me", "text")])))   # someone else's job
        self.assertIsNone(self.run_reply(job(kind=1)))
        self.assertEqual(self.bot.http.urls, [])                                                  # not even looked up

    def test_answers_p_tagged_jobs_of_other_kinds(self):
        out = self.run_reply(job(kind=5300, tags=[("p", self.bot.pub), ("param", "feed", "maritime-weather")]))
        self.assertEqual(out["kind"], 6300)

    def test_free_live_feed_gets_a_compact_result(self):
        out = self.run_reply(job(tags=[("param", "feed", "maritime_weather")]))   # alias with underscore
        self.assertEqual(out["kind"], 6990)
        content = json.loads(out["content"])
        self.assertEqual(len(content["data"]["observations"]), 10)
        self.assertEqual(content["lists_truncated"], {"$.observations": 500})
        self.assertTrue(content["full_data_url"].endswith("/api/v1/batch/maritime-weather"))
        self.assertLessEqual(len(out["content"].encode()), dvm.MAX_RESULT_BYTES)
        self.assertIn(["e", "a" * 64], out["tags"])

    def test_paid_feed_gets_l402_payment_feedback(self):
        out = self.run_reply(job(tags=[("param", "feed", "ercot-lmp")]))
        self.assertEqual(out["kind"], 7000)
        self.assertEqual(out["tags"][0][:2], ["status", "payment-required"])
        self.assertIn(["amount", "1234000"], out["tags"])
        self.assertIn("/api/v1/batch/ercot-lmp", out["tags"][0][2])

    def test_coming_soon_unknown_and_missing_feed_are_errors(self):
        for tags in ([("param", "feed", "grid")], [("param", "feed", "dark-fleet")], []):
            out = self.run_reply(job(tags=tags, jid=os.urandom(32).hex()))
            self.assertEqual(out["tags"][0][1], "error", tags)

    def test_duplicate_jobs_are_answered_once(self):
        self.assertIsNotNone(self.run_reply(job(tags=[("param", "feed", "maritime-weather")])))
        self.assertIsNone(self.run_reply(job(tags=[("param", "feed", "maritime-weather")])))

    def test_rate_limit_per_requester(self):
        limiter = dvm.RateLimiter(2)
        self.assertEqual([limiter.allow("x") for _ in range(3)], [True, True, False])
        self.assertTrue(limiter.allow("y"))

    def test_announcement_lists_only_live_feeds(self):
        evt = dvm.announcement(self.bot.key, MANIFEST)
        self.assertEqual(evt["kind"], 31990)
        self.assertEqual(sorted(t[1] for t in evt["tags"] if t[0] == "t"), ["ercot-lmp", "maritime-weather"])
        self.assertIn(["k", str(dvm.JOB_KIND)], evt["tags"])


if __name__ == "__main__":
    unittest.main()
