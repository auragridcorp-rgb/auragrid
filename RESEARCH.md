# AurAgrid pivot: research (Phase 1)

Date: 2026-10-30. Status: **awaiting go-ahead (decision point 1)**.
Backup taken before any change: `~/AurAgrid.bak-20261030-1404`.

## 0. Blockers found (fix before anything else)

1. **Possible OCI charges now.** Several outlets report Oracle cut Always Free
   Ampere A1 to **2 OCPU / 12 GB** in June 2026 (from 4 / 24), with PAYG accounts
   billed above it; enforcement reported from 18 Aug 2026. This instance is
   **VM.Standard.A1.Flex, 4 OCPU / 24 GB**. Oracle's own docs page I could read
   still shows the old 3,000 OCPU-h / 18,000 GB-h figures, so confirm in the OCI
   billing console. If confirmed: resize the instance to 2 OCPU / 12 GB (console
   action; needs a stop/start, I can't do it from inside the VM). Current RAM use
   fits: bitcoind 5.0 GB RSS (dbcache=4096, can drop to ~1 GB), LND 0.5 GB, API 0.1 GB.
   Sources: [InfoQ](https://www.infoq.com/news/2026/07/oracle-cloud-free-tier-limits/),
   [Linuxiac](https://linuxiac.com/oracle-quietly-cuts-free-tier-ampere-a1-resources-in-half/),
   [terminalbytes](https://terminalbytes.com/oracle-cloud-free-tier-changes-2026/),
   [Oracle docs](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm).
2. **The system can spend (violates rule 2).** Cron runs
   `scripts/sweep_to_vault.py` every 6 h (`lncli sendcoins` to an external
   address); it only fails today because `lncli` is not on cron's PATH.
   `dynamic_liquidity_sweep.py` (started by `~/start_auragrid_services.sh` at
   reboot) and `auragrid_swarm.py` (`payinvoice`) also move money.
3. `auragrid.db` holds 8,767 fabricated "revenue" rows (LND: 0 settled invoices ever).

## 1. Demand (who pays for small machine-readable feeds)

| Evidence | What it shows |
|---|---|
| [satring.com](https://satring.com) | 127 L402 services listed (57 live, 34 verified); **no usage or revenue figures published** |
| [Spark research](https://www.spark.money/research/bitcoin-micropayments-use-cases) | "over 100 live" L402 APIs; vendor source |
| [dev.to builder report](https://dev.to/cramberries099bit/i-launched-two-l402-apis-for-ai-agents-clean-data-for-10-sats-mempool-forensics-for-2500-1c5d) | prices 10-2,500 sats/call; author calls revenue "fine for the first sats, not where I'd keep real revenue" |
| [dvmdash.live](https://dvmdash.live) | Nostr DVM directory; job volume not publicly readable |
| This node | 11,048 L402 invoices issued since launch, **0 settled** |

**Realistic revenue: 0-20 USD/month in year one.** Buyers are mostly Bitcoin-
native developers and agent experiments; there is no public evidence of steady
paid demand for generic data. The free tier is the realistic goal (distribution,
reputation); paid endpoints should be things Bitcoin/Lightning users need and
can't trivially get elsewhere for free.

## 2. Sources (only those with clear redistribution terms are usable)

| Source | Format / access | Terms (verbatim) | Usable? |
|---|---|---|---|
| **Own bitcoind** (pruned mainnet node, already running) | JSON-RPC, local | Self-computed from the public Bitcoin network; no third-party terms | **Yes** |
| **Own LND** (already running) | REST, readonly macaroon, local | Self-computed from public Lightning gossip; no third-party terms | **Yes** |
| **U.S. EIA** Open Data API v2 [api.eia.gov/v2](https://api.eia.gov/v2) | JSON, free key (registered, verified 2026-10-31; see 2.1) | "U.S. government publications are in the public domain and are not subject to copyright protection." "You may use and/or distribute any of our data, files, databases, reports, graphs, charts, and other information products..." Credit: "Source: U.S. Energy Information Administration (Oct 2008)." ([terms](https://www.eia.gov/about/copyrights_reuse.php)) | **Yes** (key) |
| **NOAA NWS / NDBC** [api.weather.gov](https://api.weather.gov), [ndbc.noaa.gov](https://www.ndbc.noaa.gov) | JSON / text, no key | "...are in the public domain, unless specifically noted otherwise, and may be used without charge for any lawful purpose so long as you do not: 1) claim it is your own..., 2) use it in a manner that implies an endorsement or affiliation with NOAA/NWS, or 3) modify its content and then present it as official government material." ([terms](https://www.weather.gov/disclaimer)) | **Yes** |
| **USGS** earthquake GeoJSON [earthquake.usgs.gov](https://earthquake.usgs.gov/earthquakes/feed/v1.0/geojson.php) | GeoJSON, no key | "USGS-authored or produced data and information are considered to be in the U.S. Public Domain." Credit: "Credit: U.S. Geological Survey" ([terms](https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits)) | **Yes** |
| UK Carbon Intensity API | JSON, no key | Forbids: "Sell, lease, or sublicense the Carbon Intensity API or access to it." ([terms](https://github.com/carbon-intensity/terms)) | No (paid tier would breach) |
| mempool.space API | JSON | Terms page returned no readable terms | No (unclear) |
| ERCOT Public API | JSON, registration | Terms page blocks automated access (HTTP 403); not verifiable | No (unclear) |
| IMF PortWatch | ArcGIS | "For any potential commercial reuse of IMF Data, email copyright@imf.org to request permission." | No (until permission) |
| Global Fishing Watch | API | "only available for non-commercial purposes" | No |

### 2.1 U.S. EIA API v2: electricity / RTO fuel mix and regional grid metrics

100% free and real: a free key with no paid tier, public-domain data, no
simulated or mock fallback anywhere. If EIA is down, the endpoint serves the
last good snapshot flagged `stale` and then returns 503. It never makes up values.

- **Key:** `EIA_API_KEY` in `/etc/auragrid/env` (root, chmod 600). Checked with
  `curl` on 2026-10-31: HTTP 200, live US48 hourly fuel mix returned.
- **Base URL:** `https://api.eia.gov/v2/` (`<route>/data/`, max 5,000 rows per
  request, offset paging).
- **Routes used:**

| Route | Data | Facets used | AurAgrid feed |
|---|---|---|---|
| `electricity/rto/fuel-type-data` | Hourly net generation by fuel (COL, NG, NUC, OIL, WAT, SUN, WND, GEO, BAT, PS, OTH...) per respondent | `frequency=hourly` | `energy/grid` |
| `electricity/rto/region-data` | Hourly demand (D), day-ahead demand forecast (DF), net generation (NG), total interchange (TI) | `type` = D, DF, NG, TI | `energy/grid` |
| `petroleum/stoc/wstk` | Weekly SPR + commercial crude stocks | series WCSSTUS1, WCESTUS1 | `spr-stockpile` |
| `natural-gas/move/poe2` | Monthly LNG export volumes (ENG) and prices (PNG) by terminal/destination | process ENG, PNG | `lng-supply-chain` |

- **Coverage:** Lower 48 total, EIA's 13 regions (CAL, CAR, CENT, FLA, MIDA,
  MIDW, NE, NY, NW, SE, SW, TEN, TEX) and every balancing authority (RTOs:
  CISO, ERCO, MISO, PJM, NYIS, ISNE, SWPP...): 70 respondents on 2026-10-31.
- **Latency (observed 2026-10-31 18:26 UTC):** region demand under 8 h behind (rows present for the last 8 h);
  **fuel mix about 12 h behind** (newest hour 06:00 UTC). The connector anchors
  its window on EIA's newest published fuel-mix hour, not the wall clock.
- **Derived metrics:** renewable, carbon-free and fossil shares of positive net
  generation (excluding storage); the 10 cleanest balancing authorities.
- **Attribution:** "Source: U.S. Energy Information Administration (EIA)" plus
  "EIA does not endorse AurAgrid" on every response (`provenance`).

## 3. Proposed product (7 endpoints, derived/aggregated)

| # | Endpoint | Source | Derived metrics | Tier |
|---|---|---|---|---|
| 1 | `/api/v1/btc/fees` | own bitcoind | fee estimates for 1/3/6/12/144 blocks, mempool size/vsize/min fee, last-block fee stats | **Paid** (fresh, 1 min); free copy 10 min delayed |
| 2 | `/api/v1/ln/network` | own LND graph | node/channel counts, capacity, fee-rate & base-fee percentiles, channel size distribution, 24 h change | **Paid** (hourly); free daily snapshot |
| 3 | `/api/v1/energy/grid` (built) | EIA-930 | demand, generation mix, renewable/carbon-free shares by region | Free (EIA key active) |
| 4 | `/api/v1/batch/spr-stockpile` (built) | EIA weekly | SPR + commercial crude, 1/4/52-week changes | Free (EIA key active) |
| 5 | `/api/v1/batch/lng-supply-chain` (built) | EIA monthly | U.S. LNG exports by terminal/destination, YoY | Free (EIA key active) |
| 6 | `/api/v1/batch/maritime-weather` (live) | NOAA NDBC | ~865 stations, summary extremes | Free |
| 7 | `/api/v1/hazards/earthquakes` | USGS | M2.5+ last 24 h / 7 d counts, largest events, events near energy/port regions (bounding boxes) | Free |

Retire (HTTP 410): `ercot-lmp` (terms unverifiable), `port-congestion-index`
(IMF permission), and all 17 catalog feeds with no source.

## 4. Pricing (sat-denominated, small)

| Product | Price | Mechanism |
|---|---|---|
| `btc/fees` hourly token | 50 sats | existing Aperture hourly rule |
| `ln/network` hourly token | 100 sats | existing Aperture hourly rule |
| **"Lightning Operator" 30-day bundle** (fees + network) | 5,000 sats | existing `pass_support` bundle (static price, works across both paths) |
| everything else | free | `free-feeds` rule, 30 req/min/IP |

Free copies of the paid feeds (10-min-delayed fees, daily LN snapshot) keep the
free tier useful and make the paid freshness the only thing you pay for.

## 5. Cost & resource notes

- Egress: ~17 GB/month today vs 10 TB free; gzip already on (NDBC 418 KB -> 40 KB).
  bitcoind serves peers on 0.0.0.0:8333 (outbound bandwidth); recommend `listen=0`.
- Disk: 37 GB of 194 GB used (bitcoind 21 GB pruned). App data target < 2 GB.
- New paid feeds read only local nodes (no upstream calls, no keys).

## 6. Manual steps for you (no accounts registered by me)

1. Confirm/resize OCI instance to 2 OCPU / 12 GB if billing shows A1 charges.
2. ~~Free EIA API key (endpoints 3-5)~~ done 2026-10-31, stored in `/etc/auragrid/env`.
3. Optional: email copyright@imf.org (PortWatch), TLS domain for HTTPS.
