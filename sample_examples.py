"""Labeled schema examples for feeds that are not connected yet.

Served on /api/v1/sample/<feed> only while no real provider is registered, so a
buyer (or agent) can inspect the response structure before paying. Every value
is made up, and the response says so in three places ("mode": "schema_example",
"example": true, "synthetic": true) plus a plain-language notice. Once a feed is
connected, its sample route serves delayed real data instead.

tests/test_sample_examples.py checks that each example has exactly the keys the
real provider produces, so the two cannot drift apart.
"""

NOTICE = ("SCHEMA EXAMPLE ONLY: every value below is synthetic and illustrates the response "
          "structure. It is not market data. The paid feed is not connected yet.")

EXAMPLES = {
    "ercot-lmp": {
        "source": "ERCOT NP6-788-CD: LMPs by Resource Nodes, Load Zones and Trading Hubs (real-time SCED)",
        "sced_timestamp": "2000-01-01T00:00:00",
        "timezone": "America/Chicago",
        "repeated_hour": False,
        "settlement_points": 3,
        "summary": {"min_lmp": 10.0, "max_lmp": 30.0, "mean_lmp": 20.0, "unit": "USD/MWh"},
        "trading_hubs": {"HB_EXAMPLE": 20.0},
        "load_zones": {"LZ_EXAMPLE": 30.0},
        "prices": [{"settlement_point": "EXAMPLE_NODE", "lmp": 10.0},
                   {"settlement_point": "HB_EXAMPLE", "lmp": 20.0},
                   {"settlement_point": "LZ_EXAMPLE", "lmp": 30.0}],
        "fetched_at": "2000-01-01T00:00:00+00:00",
        "stale": False,
    },
    "lng-supply-chain": {
        "dataset": "AurAgrid U.S. LNG Export Monitor",
        "period": "2000-01",
        "frequency": "monthly",
        "notes": "U.S. LNG exports by terminal and destination from EIA monthly data.",
        "us_total": {"mmcf": 300000.0, "bcf_per_day": 9.677, "mom_pct": 1.0, "yoy_pct": 10.0,
                     "trailing_12m_mmcf": 3600000.0, "export_price_usd_per_mcf": 5.0},
        "terminals": [{"terminal": "Example Terminal, XX", "eia_area": "YEXA-Z00", "mmcf": 100000.0,
                       "bcf_per_day": 3.226, "share_pct": 33.3, "yoy_pct": 5.0, "export_price_usd_per_mcf": 5.0}],
        "destinations": [{"country": "Exampleland", "iso3": "XXX", "mmcf": 50000.0, "share_pct": 16.7,
                          "yoy_pct": -2.0}],
        "history": [{"period": "2000-01", "mmcf": 300000.0, "bcf_per_day": 9.677}],
        "attribution": {"source": "Source: U.S. Energy Information Administration (EIA).",
                        "terms": "EIA data is in the U.S. public domain. EIA does not endorse AurAgrid.",
                        "transformation": "AurAgrid computes Bcf/d, shares and percentage changes."},
        "fetched_at": "2000-04-01T00:00:00+00:00",
        "stale": False,
    },
    "port-congestion-index": {
        "index": "AurAgrid Top 100 Port Activity Index",
        "data_as_of": "2000-01-07",
        "recent_window": ["2000-01-01", "2000-01-07"],
        "baseline_window": ["1999-10-03", "1999-12-31"],
        "notes": "Ratios compare daily averages: last 7 days vs the prior 90 days (1.0 = normal).",
        "global": {"ports": 1, "activity_ratio": 1.0, "import_ratio": 1.0, "export_ratio": 1.0},
        "chokepoints": [{"chokepoint": "Example Strait", "transits_7d": 70, "tanker_transits_7d": 20,
                         "container_transits_7d": 30, "transit_ratio": 1.0, "capacity_ratio": 1.0}],
        "ports": [{"port_id": "port0", "port": "Example Port", "country": "Exampleland", "iso3": "XXX",
                   "locode": "XXXXX", "port_calls_7d": 70, "port_calls_daily_avg_7d": 10.0,
                   "port_calls_daily_avg_90d": 10.0, "activity_ratio": 1.0, "import_tonnes_7d": 700000,
                   "import_ratio": 1.0, "export_tonnes_7d": 700000, "export_ratio": 1.0,
                   "tanker_calls_7d": 14, "container_calls_7d": 28, "rank": 1}],
        "attribution": {"source": "Source: International Monetary Fund, PortWatch (portwatch.imf.org).",
                        "free_from_source": "The underlying PortWatch data is available free of charge from the IMF.",
                        "transformation": "Aggregated into 7-day totals and ratios.",
                        "terms": "https://www.imf.org/en/About/copyright-and-terms"},
        "fetched_at": "2000-01-07T00:00:00+00:00",
        "stale": False,
    },
}


def example_body(key: str):
    data = EXAMPLES.get(key)
    if data is None:
        return None
    return {"feed": key, "mode": "schema_example", "example": True, "synthetic": True,
            "notice": NOTICE, "data": data}
