"""Free tier: access config, routes, manifest, terms, compression, Aperture rules, NDBC parsing."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
import feeds  # noqa: E402
import gen_aperture  # noqa: E402
import ndbc_feed  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pass_support import load_access, parse_plans  # noqa: E402

BIG = {"rows": [{"station": "S%04d" % i, "value": i * 1.5} for i in range(2000)]}


class FreeTierRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        feeds.provider("maritime-weather")(lambda: BIG)
        cls.client = TestClient(t.backend.app)

    @classmethod
    def tearDownClass(cls):
        feeds._PROVIDERS.pop("maritime-weather", None)

    def test_free_feed_needs_no_payment_or_gateway(self):
        r = self.client.get("/api/v1/batch/maritime-weather")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.json()["data"]["rows"]), 2000)   # free = full data, not a sample

    def test_paid_feed_still_needs_gateway(self):
        self.assertEqual(self.client.get("/api/v1/batch/btc-fees").status_code, 403)

    def test_feeds_not_offered_have_no_route(self):
        self.assertEqual(self.client.get("/api/v1/batch/no-such-feed", headers=t.GW).status_code, 404)
        self.assertNotIn("/api/v1/batch/no-such-feed", self.client.get("/openapi.json").json()["paths"])
        # retired feeds answer 410 (tests/test_plans_samples.py) and are not advertised
        self.assertNotIn("/api/v1/batch/naval-escalation", self.client.get("/openapi.json").json()["paths"])

    def test_manifest_claims_signatures_only_when_signing(self):
        m = self.client.get("/agent-manifest.json").json()   # tests run without AURAGRID_ATTEST_MACAROON
        self.assertFalse(m["signature"]["enabled"])
        self.assertEqual(m["signature"]["header"], "X-AurAgrid-Signature-V2")
        self.assertNotIn("Signature-V2", m["description_for_model"])
        self.assertIn("serves %d data endpoints" % m["endpoints_count"], m["description_for_model"])

    def test_manifest_shows_access_and_status(self):
        endpoints = {e["path"]: e for e in self.client.get("/agent-manifest.json").json()["endpoints"]}
        self.assertNotIn("/api/v1/batch/naval-escalation", endpoints)
        weather = endpoints["/api/v1/batch/maritime-weather"]
        self.assertEqual((weather["access"], weather["auth"], weather["status"]), ("free", "none", "live"))
        self.assertIn("rate_limit", weather)
        self.assertNotIn("price_sats", weather)
        self.assertEqual(endpoints["/api/v1/batch/btc-fees"]["access"], "paid")
        self.assertEqual(endpoints["/api/v1/batch/spr-stockpile"]["status"], "coming_soon")   # no snapshot stored yet

    def test_terms_and_llms(self):
        terms = self.client.get("/terms").text
        self.assertIn("30 requests per minute", terms)
        self.assertIn("90 days", terms)
        self.assertIn("status: coming_soon", self.client.get("/llms.txt").text)

    def test_large_responses_are_gzipped(self):
        r = self.client.get("/api/v1/batch/maritime-weather", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.headers.get("content-encoding"), "gzip")


class FreeTierConfigTests(unittest.TestCase):
    def setUp(self):
        import yaml
        with open(os.path.join(t.ROOT, "aperture.base.yaml")) as fh:
            self.base = yaml.safe_load(fh)
        fixture = os.environ["AURAGRID_PRICING_CONFIG"]
        self.access = load_access(fixture)
        self.plans = []

    def test_generated_rules(self):
        config = gen_aperture.build(self.base, self.plans, 77000.0, "s" * 40, self.access)
        self.assertEqual(gen_aperture.validate(config, self.plans, self.access), [])
        names = [s["name"] for s in config["services"]]
        self.assertEqual(names[0], "free-feeds")
        self.assertEqual(names[-1], "public-free")
        self.assertIn("batch-btc-fees", names)
        self.assertNotIn("batch-naval-escalation", names)          # retired: not sold
        self.assertNotIn("batch-lng-supply-chain", names)          # free: no paid rule
        free = config["services"][0]
        self.assertEqual(free["auth"], "off")
        self.assertNotIn("headers", free)                          # no gateway secret on free rules
        self.assertEqual(free["ratelimits"][0]["requests"], 30)
        rx = re.compile(free["pathregexp"])
        self.assertTrue(rx.match("/api/v1/batch/maritime-weather"))
        self.assertFalse(rx.match("/api/v1/batch/btc-fees"))
        self.assertFalse(rx.match("/api/v1/batch/maritime-weather/../btc-fees"))
        self.assertIn("ratelimits", config["services"][-1])
        self.assertTrue(re.match(config["services"][-1]["pathregexp"], "/terms"))

    def test_paid_feed_without_rule_is_an_error(self):
        from pass_support import Access
        access = Access(free=(), paid=("no-such-feed",))
        config = gen_aperture.build(self.base, [], 77000.0, None, access)
        self.assertTrue(any("no-such-feed" in p for p in gen_aperture.validate(config, [], access)))

    def test_feed_cannot_be_free_and_paid(self):
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "p.yaml")
        with open(path, "w") as fh:
            fh.write("access:\n  free: {feeds: [a]}\n  paid: {feeds: [a]}\n")
        with self.assertRaises(ValueError):
            load_access(path)

    def test_disabled_plans_are_skipped(self):
        plans = parse_plans({"p": {"enabled": False, "kind": "pass_30d", "prefix": "pro", "feeds": ["x"],
                                   "usd": 1, "timeout": 10}})
        self.assertEqual(plans, [])


OBS = """#STN       LAT      LON  YYYY MM DD hh mm WDIR WSPD   GST WVHT  DPD APD MWD   PRES  PTDY  ATMP  WTMP  DEWP  VIS   TIDE
#text      deg      deg   yr mo day hr mn degT  m/s   m/s   m   sec sec degT   hPa   hPa  degC  degC  degC  nmi     ft
41001    34.724  -72.317 2026 10 30 12 00 200   9.0  11.0  2.1   8 6.0 190 1015.2  -0.4  20.1  24.0    MM   MM     MM
46042    36.785 -122.396 2026 10 30 12 10  MM    MM    MM  4.5  14 9.1 300     MM    MM    MM  15.2    MM   MM     MM
"""
STATIONS = """<stations><station id="41001" lat="34.7" lon="-72.3" name="EAST HATTERAS" owner="NDBC" type="buoy"/>
<station id="46042" lat="36.8" lon="-122.4" name="MONTEREY" owner="NDBC" type="buoy"/></stations>"""


class NDBCTests(unittest.TestCase):
    def test_snapshot(self):
        snap = ndbc_feed.build_snapshot(OBS, ndbc_feed.parse_stations(STATIONS))
        self.assertEqual(snap["stations"], 2)
        first = snap["observations"][0]
        self.assertEqual((first["station"], first["name"], first["wind_speed_ms"], first["dewpoint_c"]),
                         ("41001", "EAST HATTERAS", 9.0, None))
        self.assertEqual(snap["summary"]["max_wave_height"]["station"], "46042")
        self.assertEqual(snap["summary"]["stations_waves_over_4m"], 1)
        self.assertEqual(snap["observed_between"], ["2026-10-30T12:00+00:00", "2026-10-30T12:10+00:00"])
        self.assertIn("NOAA", snap["attribution"]["source"])

    def test_empty_file_is_an_error(self):
        with self.assertRaises(RuntimeError):
            ndbc_feed.build_snapshot(OBS.splitlines()[0] + "\n", {})


if __name__ == "__main__":
    unittest.main()


class EgressGuardTests(unittest.TestCase):
    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("egress_guard", os.path.join(t.ROOT, "scripts", "egress_guard.py"))
        self.g = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.g)

    def test_counts_across_runs_reboots_and_months(self):
        s = self.g.update({}, 100, "boot1", "2026-10")
        s = self.g.update(s, 250, "boot1", "2026-10")
        self.assertEqual(s["total"], 250)
        s = self.g.update(s, 40, "boot2", "2026-10")          # reboot: counter restarted
        self.assertEqual(s["total"], 290)
        s = self.g.update(s, 90, "boot2", "2026-11")          # new month starts from the delta
        self.assertEqual(s["total"], 50)
