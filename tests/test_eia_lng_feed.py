"""Offline tests for the EIA LNG provider (no network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eia_lng_feed as eia  # noqa: E402


def row(area, period, value, process="ENG", desc="", name="NA"):
    return {"duoarea": area, "period": period, "value": value, "process": process,
            "series-description": desc, "area-name": name}


def sample_rows():
    rows = []
    for i in range(25):
        period = eia._shift("2026-07", -i)
        us = 500000 if i == 0 else (490000 if i == 1 else 450000)
        rows += [
            row("NUS-Z00", period, us, desc="Liquefied U.S. Natural Gas Exports (MMcf)", name="U.S."),
            row("YSPL-Z00", period, us * 0.25, desc="Sabine Pass, LA Liquefied Natural Gas Exports (Million Cubic Feet)"),
            row("NUS-NEG", period, us * 0.15, desc="Liquefied U.S. Natural Gas Exports to Egypt (MMcf)", name="EGY"),
            row("YSPL-NEG", period, us * 0.05, desc="Sabine Pass, LA Liquefied Natural Gas Exports to Egypt"),
        ]
    rows += [row("NUS-Z00", "2026-07", "4.5", process="PNG"), row("YSPL-Z00", "2026-07", "4.4", process="PNG"),
             row("NUS-NXX", "2026-07", None, desc="Liquefied U.S. Natural Gas Exports to Nowhere (MMcf)")]
    return rows


class EiaTests(unittest.TestCase):
    def test_snapshot(self):
        snap = eia.build_snapshot(sample_rows())
        self.assertEqual(snap["period"], "2026-07")
        us = snap["us_total"]
        self.assertEqual(us["mmcf"], 500000)
        self.assertEqual(us["bcf_per_day"], round(500000 / 1000 / 31, 3))
        self.assertEqual(us["mom_pct"], round((500000 / 490000 - 1) * 100, 1))
        self.assertEqual(us["yoy_pct"], round((500000 / 450000 - 1) * 100, 1))
        self.assertEqual(us["export_price_usd_per_mcf"], 4.5)
        self.assertEqual([t["terminal"] for t in snap["terminals"]], ["Sabine Pass, LA"])   # totals only
        self.assertEqual(snap["terminals"][0]["share_pct"], 25.0)
        self.assertEqual(snap["destinations"][0]["country"], "Egypt")
        self.assertEqual(len(snap["destinations"]), 1)                                     # blank row skipped
        self.assertEqual(len(snap["history"]), 24)
        self.assertEqual(snap["history"][0]["period"], "2026-07")

    def test_no_totals_is_an_error(self):
        with self.assertRaises(RuntimeError):
            eia.build_snapshot([])

    def test_not_registered_without_key(self):
        os.environ.pop("EIA_API_KEY", None)
        self.assertFalse(eia.register_from_env())

    def test_month_shift(self):
        self.assertEqual(eia._shift("2026-01", -1), "2025-12")
        self.assertEqual(eia._shift("2026-07", -12), "2025-07")


if __name__ == "__main__":
    unittest.main()
