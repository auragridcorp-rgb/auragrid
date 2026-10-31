"""Offline tests for the EIA-930 grid feed, the SPR feed and the shared EIA client."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eia_api  # noqa: E402
import eia_grid_feed as grid  # noqa: E402
import eia_spr_feed as spr  # noqa: E402


def fuel(resp, period, code, value, name=None):
    return {"respondent": resp, "respondent-name": name or resp, "period": period, "fueltype": code,
            "value": value}


def region(resp, period, typ, value):
    return {"respondent": resp, "respondent-name": resp, "period": period, "type": typ, "value": value}


class GridTests(unittest.TestCase):
    def rows(self):
        f = [
            # US48 at 12 complete; 13 is partial (only NG reported yet) and must be ignored
            fuel("US48", "2026-10-30T12", "NG", 200), fuel("US48", "2026-10-30T12", "WND", 100),
            fuel("US48", "2026-10-30T12", "NUC", 100), fuel("US48", "2026-10-30T12", "BAT", -20),
            fuel("US48", "2026-10-30T13", "NG", 210),
            # a hydro-heavy BA with a code we don't know (counted in total, in no share)
            fuel("BPAT", "2026-10-30T11", "WAT", 900, "Bonneville"), fuel("BPAT", "2026-10-30T11", "NG", 50),
            fuel("BPAT", "2026-10-30T11", "XYZ", 50),
            fuel("TEX", "2026-10-30T12", "NG", 800), fuel("TEX", "2026-10-30T12", "SUN", 200),
            fuel("TINY", "2026-10-30T12", "SUN", 10),
            fuel("NULL", "2026-10-30T12", "NG", None),
        ]
        r = [region("US48", "2026-10-30T12", "D", 410), region("US48", "2026-10-30T12", "DF", 405),
             region("US48", "2026-10-30T12", "NG", 400), region("US48", "2026-10-30T12", "TI", -10)]
        return r, f

    def test_snapshot(self):
        snap = grid.build_snapshot(*self.rows())
        us = snap["lower48"]
        self.assertEqual(us["period_utc"], "2026-10-30T12")       # complete hour, not the partial 13
        self.assertEqual(us["demand_mwh"], 410)
        self.assertEqual(us["renewable_share_pct"], 25.0)         # 100 wind / 400 (battery excluded)
        self.assertEqual(us["carbon_free_share_pct"], 50.0)
        self.assertEqual(us["fossil_share_pct"], 50.0)
        kinds = {r["respondent"]: r["kind"] for r in snap["regions"]}
        self.assertEqual((kinds["US48"], kinds["TEX"], kinds["BPAT"]), ("lower48", "eia_region", "balancing_authority"))
        self.assertNotIn("NULL", kinds)
        bpat = next(r for r in snap["regions"] if r["respondent"] == "BPAT")
        self.assertEqual(bpat["renewable_share_pct"], 90.0)        # unknown code counted in total only
        cleanest = [r["respondent"] for r in snap["cleanest_balancing_authorities"]]
        self.assertEqual(cleanest, ["BPAT"])                        # TINY under 500 MWh, TEX is a region
        self.assertIn("EIA-930", snap["attribution"]["source"])

    def test_no_data_is_an_error(self):
        with self.assertRaises(RuntimeError):
            grid.build_snapshot([], [])


class SprTests(unittest.TestCase):
    def rows(self):
        out = []
        for i in range(60):
            week = "2026-%02d-%02d" % (1 + (59 - i) // 5, 1 + (59 - i) % 5)   # unique, ordered periods
            out.append({"series": "WCSSTUS1", "period": week, "value": str(400000 - i * 500)})
            out.append({"series": "WCESTUS1", "period": week, "value": str(420000 + i * 100)})
        out.append({"series": "WCSSTUS1", "period": "1999-01-01", "value": None})
        return out

    def test_snapshot(self):
        snap = spr.build_snapshot(self.rows())
        s = snap["spr"]
        self.assertEqual(s["million_barrels"], 400.0)
        self.assertEqual(s["change_1w_million_barrels"], 0.5)      # 400.0 - 399.5
        self.assertEqual(s["change_4w_million_barrels"], 2.0)
        self.assertEqual(s["change_52w_million_barrels"], 26.0)
        self.assertEqual(s["range_52w_million_barrels"], [374.5, 400.0])
        self.assertEqual(snap["commercial_crude"]["change_1w_million_barrels"], -0.1)
        self.assertIsNone(snap["total_crude_incl_spr"])            # series absent: reported as null
        self.assertEqual(len(snap["history"]), 52)
        self.assertEqual(snap["history"][0]["week_ending"], s["week_ending"])

    def test_no_spr_is_an_error(self):
        with self.assertRaises(RuntimeError):
            spr.build_snapshot([{"series": "WCESTUS1", "period": "2026-01-01", "value": "1"}])


class FakeResp:
    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class EiaClientTests(unittest.TestCase):
    def test_paging_and_errors(self):
        pages = [{"response": {"total": 6000, "data": [{"i": i} for i in range(5000)]}},
                 {"response": {"total": 6000, "data": [{"i": i} for i in range(1000)]}}]
        calls = []

        class S:
            def get(self, url, params=None, timeout=None):
                calls.append(params["offset"])
                return FakeResp(pages[len(calls) - 1])

        self.assertEqual(len(eia_api.get_rows(S(), "x/y", "k", {})), 6000)
        self.assertEqual(calls, [0, 5000])

        class Limited:
            def get(self, url, params=None, timeout=None):
                return FakeResp({"error": {"code": "OVER_RATE_LIMIT"}})

        with self.assertRaises(eia_api.EIAError):
            eia_api.get_rows(Limited(), "x/y", "k", {}, sleep=lambda s: None)

    def test_grid_window_follows_newest_fuel_hour(self):
        # Fuel mix published up to 06:00 while the wall clock is much later: the
        # window must start 8 h before 06:00, not 8 h before now.
        calls = []

        class S:
            def get(self, url, params=None, timeout=None):
                calls.append((url, params))
                if params["length"] == 1:
                    return FakeResp({"response": {"total": 1, "data": [{"period": "2026-10-31T06"}]}})
                if "fuel-type-data" in url:
                    data = [fuel("US48", "2026-10-31T06", "NG", 100), fuel("US48", "2026-10-31T06", "WND", 50)]
                else:
                    data = [region("US48", "2026-10-31T06", "D", 140)]
                return FakeResp({"response": {"total": len(data), "data": data}})

        snap = grid.fetch(S(), "k")
        self.assertEqual(snap["lower48"]["period_utc"], "2026-10-31T06")
        self.assertEqual(snap["lower48"]["demand_mwh"], 140)
        starts = {p["start"] for _, p in calls if "start" in p}
        self.assertEqual(starts, {"2026-10-30T22"})

    def test_not_registered_without_key(self):
        os.environ.pop("EIA_API_KEY", None)
        self.assertFalse(grid.register_from_env())
        self.assertFalse(spr.register_from_env())


if __name__ == "__main__":
    unittest.main()
