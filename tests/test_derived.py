"""Derived endpoints (machine-discovery Phase 4): maths, stale gates, routing."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import test_plans_samples as t  # noqa: E402  (sets env, stubs providers, imports the app)
import derived  # noqa: E402
import gen_aperture  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pass_support import load_access, load_plans  # noqa: E402
from store import Store  # noqa: E402

NOW = 1_800_000_000.0


def btc(fee6, ts_iso="2026-01-01T00:00:00+00:00"):
    return {"observed_at": ts_iso, "fee_estimates": {"1": {"sat_per_vb": fee6 * 2}, "6": {"sat_per_vb": fee6},
                                                     "144": {"sat_per_vb": 1.0}},
            "mempool": {"min_fee_sat_per_vb": 1.0}}


class Clocked:
    """A Store whose writes happen at a chosen time."""

    def __init__(self):
        self.now = NOW
        self.store = Store(os.path.join(tempfile.mkdtemp(), "feeds.db"), clock=lambda: self.now)

    def save(self, feed, data, as_of, at):
        self.now = at
        self.store.save(feed, data, as_of)
        self.now = NOW


class FeeTimingTests(unittest.TestCase):
    def test_percentiles_trend_and_lowest(self):
        c = Clocked()
        # 24 hours of hourly observations: fee falls from 24 to 1 over the day.
        for i in range(24):
            ts = NOW - (23 - i) * 3600
            c.save("btc-fees", btc(24.0 - i), "obs-%02d" % i, ts)
        r = derived.fee_timing(c.store, clock=lambda: NOW)
        d = r["data"]
        self.assertEqual(d["current"]["sat_per_vb"]["6"], 1.0)
        self.assertEqual(d["last_24h"]["min"], 1.0)
        self.assertEqual(d["last_24h"]["max"], 24.0)
        self.assertEqual(d["last_24h"]["current_percentile_rank"], round(100 / 24, 1))
        self.assertEqual(d["last_24h"]["lowest"]["sat_per_vb"], 1.0)
        self.assertIsNone(d["trend"])   # fewer than 10 points in the last 6 hours
        self.assertEqual(r["method"], "derived")
        self.assertIn("least-squares", r["provenance"]["derivation"])
        self.assertFalse(r["stale"])

    def test_trend_direction(self):
        c = Clocked()
        for i in range(60):   # every 5 minutes for 5 hours, rising
            c.save("btc-fees", btc(10.0 + i * 0.5), "r%02d" % i, NOW - (59 - i) * 300)
        self.assertEqual(derived.fee_timing(c.store, clock=lambda: NOW)["data"]["trend"]["direction"], "rising")

    def test_stale_and_missing_input(self):
        c = Clocked()
        with self.assertRaises(derived.InputUnavailable):
            derived.fee_timing(c.store, clock=lambda: NOW)
        c.save("btc-fees", btc(5), "old", NOW - 3600)        # 1 h > 15 min stale limit, < 6 h max
        self.assertTrue(derived.fee_timing(c.store, clock=lambda: NOW)["stale"])
        c2 = Clocked()
        c2.save("btc-fees", btc(5), "older", NOW - 7 * 3600)  # beyond the 6 h max age
        with self.assertRaises(derived.InputUnavailable):
            derived.fee_timing(c2.store, clock=lambda: NOW)


class AreaRiskTests(unittest.TestCase):
    def setUp(self):
        self.c = Clocked()
        quakes = {"events": {
            "m2_5_plus_24h": [{"id": "a", "magnitude": 3.1, "time": "t", "lat": 29.5, "lon": -90.0,
                               "depth_km": 10, "tsunami_flag": False}],
            "m4_5_plus_7d": [{"id": "b", "magnitude": 5.4, "time": "t", "lat": 30.0, "lon": -89.0,
                              "depth_km": 20, "tsunami_flag": True},
                             {"id": "far", "magnitude": 7.0, "time": "t", "lat": -30.0, "lon": 150.0,
                              "depth_km": 20, "tsunami_flag": True}]}}
        marine = {"observations": [
            {"station": "s1", "lat": 29.0, "lon": -89.5, "wave_height_m": 4.5, "wind_speed_ms": 12.0,
             "gust_ms": 18.0, "pressure_hpa": 1004.0, "observed_at": "t"},
            {"station": "far", "lat": 60.0, "lon": 0.0, "wave_height_m": 9.0, "wind_speed_ms": 30.0,
             "gust_ms": 35.0, "pressure_hpa": 960.0, "observed_at": "t"}]}
        self.c.save("earthquakes", quakes, "q", NOW - 60)
        self.c.save("maritime-weather", marine, "m", NOW - 60)

    def test_counts_and_flags_inside_the_radius_only(self):
        d = derived.area_risk(self.c.store, 29.0, -90.0, 300, clock=lambda: NOW)["data"]
        self.assertEqual(d["earthquakes"]["m2_5_plus_24h"]["count"], 1)
        self.assertEqual(d["earthquakes"]["m4_5_plus_7d"]["count"], 1)
        self.assertEqual(d["marine"]["stations_in_radius"], 1)
        self.assertEqual(d["marine"]["max_wave_height_m"]["value"], 4.5)
        self.assertEqual(d["flags"], {"earthquake_m5_plus_7d": True, "earthquake_tsunami_flag_7d": True,
                                      "waves_over_4m": True, "gale_force_gust": True, "storm_force_wind": False})

    def test_distance_is_great_circle(self):
        self.assertAlmostEqual(derived._km(0, 0, 0, 1), 111.2, delta=0.2)

    def test_bounds(self):
        with self.assertRaises(ValueError):
            derived.area_risk(self.c.store, 95, 0, 100, clock=lambda: NOW)


class GridCarbonTests(unittest.TestCase):
    def test_weighted_intensity_excludes_storage_and_unknown(self):
        mix = {"COL": 100.0, "NG": 100.0, "NUC": 200.0, "BAT": 50.0, "UNK": 50.0, "PS": -10.0}
        out = derived._intensity(mix)
        expected = (100 * 2.31 + 100 * 0.96) * derived.LB_TO_G / 400
        self.assertAlmostEqual(out["g_co2_per_kwh"], round(expected, 1))
        self.assertEqual(out["generation_counted_mwh"], 400.0)
        self.assertEqual(out["excluded_share_pct"], 20.0)

    def test_document_cites_the_factor_table(self):
        c = Clocked()
        c.save("energy/grid", {"lower48": {"period_utc": "p", "generation_by_fuel_mwh": {"NG": 10.0}}, "regions": []},
               "p", NOW - 60)
        r = derived.grid_carbon(c.store, clock=lambda: NOW)
        self.assertEqual(r["provenance"]["emission_factors"]["url"], derived.EIA_CO2_FAQ)
        self.assertIn("EIA does not endorse", r["provenance"]["attribution"])
        self.assertAlmostEqual(r["data"]["lower48"]["g_co2_per_kwh"], round(0.96 * derived.LB_TO_G, 1))


class GridCarbonFreshnessTests(unittest.TestCase):
    def test_grid_carbon_is_as_fresh_as_the_mix(self):
        from datetime import datetime, timezone
        c = Clocked()
        hour = lambda h: datetime.fromtimestamp(NOW - h * 3600, timezone.utc).strftime("%Y-%m-%dT%H")  # noqa: E731
        for lag, stale in ((30, False), (40, True)):
            c.save("energy/grid", {"lower48": {"period_utc": hour(lag), "generation_by_fuel_mwh": {"NG": 10.0}},
                                   "regions": [], "realtime": {"period_utc": hour(0)}}, "g%d" % lag, NOW - 60)
            r = derived.grid_carbon(c.store, clock=lambda: NOW)
            self.assertEqual(r["stale"], stale, lag)
            self.assertEqual(r["provenance"]["inputs"][0]["as_of"], hour(lag))

    def test_energy_snapshot_uses_realtime_demand(self):
        c = Clocked()
        c.save("energy/grid", {"lower48": {"period_utc": "2026-01-01T00", "demand_mwh": 1, "renewable_share_pct": 40.0},
                               "generation_mix_lag_hours": 30,
                               "realtime": {"lower48": {"period_utc": "2026-01-02T06", "demand_mwh": 2}}}, "g", NOW - 60)
        c.save("spr-stockpile", {"spr": {}}, "s", NOW - 60)
        c.save("lng-supply-chain", {"us_total": {}, "period": "p"}, "l", NOW - 60)
        e = derived.energy_snapshot(c.store, clock=lambda: NOW)["data"]["electricity_lower48"]
        self.assertEqual((e["period_utc"], e["demand_mwh"]), ("2026-01-02T06", 2))
        self.assertEqual(e["generation_mix"], {"period_utc": "2026-01-01T00", "lag_hours": 30,
                                               "renewable_share_pct": 40.0, "carbon_free_share_pct": None,
                                               "fossil_share_pct": None})


class EnergySnapshotTests(unittest.TestCase):
    def test_parts_keep_their_own_freshness(self):
        c = Clocked()
        c.save("energy/grid", {"lower48": {"demand_mwh": 1}}, "g", NOW - 60)
        c.save("spr-stockpile", {"spr": {"million_barrels": 1}}, "s", NOW - 11 * 86400)   # > 10 d stale limit
        c.save("lng-supply-chain", {"us_total": {"mmcf": 1}, "period": "2026-08"}, "l", NOW - 60)
        r = derived.energy_snapshot(c.store, clock=lambda: NOW)
        self.assertTrue(r["stale"])
        self.assertTrue(r["data"]["parts"]["crude_stocks"]["stale"])
        self.assertFalse(r["data"]["parts"]["electricity_lower48"]["stale"])


class RoutingTests(unittest.TestCase):
    def test_derived_routes_answer_503_when_an_input_is_missing(self):
        client = TestClient(t.backend.app)
        t.backend._derived_cache.clear()
        r = client.get("/api/v1/derived/area-risk?lat=29&lon=-90")   # test store has no marine data
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.json()["stale"])
        self.assertEqual(client.get("/api/v1/derived/area-risk?lat=99&lon=0").status_code, 422)

    def test_aperture_serves_derived_paths_free(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        base = yaml.safe_load(open(os.path.join(here, "aperture.base.yaml")))
        pricing = os.path.join(here, "pricing.yaml")
        cfg = gen_aperture.build(base, load_plans(pricing), 100000.0, None, load_access(pricing))
        free = next(s for s in cfg["services"] if s["name"] == gen_aperture.FREE_FEEDS_SERVICE)
        import re
        for key in derived.ENDPOINTS:
            self.assertTrue(re.match(free["pathregexp"], "/api/v1/" + key), key)
        self.assertEqual(free["auth"], "off")
