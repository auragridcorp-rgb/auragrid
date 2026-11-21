# Changelog

AurAgrid's pivot to a lean, free-tier L402 data oracle (plan: `RESEARCH.md`;
current state: `PROJECT_STATE.md`). Newest first.

## 2026-11-21: dashboard status fix, listings

- **energy/grid fresh again.** EIA's fuel mix, net generation and interchange were 30 h
  behind while measured demand was current, and the feed was dated by the fuel mix. New
  `realtime` block (measured demand at EIA's newest hour; Lower-48 totals only when all 13
  regions reported, otherwise null with the reason; forecasts excluded, since recent-hour
  forecasts were partial sums) dates the feed; the mix keeps its own hour and
  `generation_mix_lag_hours`. `grid-carbon` is dated by the mix and goes stale past 36 h;
  `energy-snapshot` shows current demand next to the mix shares at their hour.
- `verify_deployment.py` had been replaced at 08:25 by an old-catalog version (swept into
  commit d7ae187); restored to 72a0d06 (43 PASS, 0 WARN, 0 FAIL). The replaced copy is in
  `~/AurAgrid.bak-20261119-1241/verify_deployment.py.replaced-20261121-0825`.

- **Dashboard:** "Node API: Unreachable" appeared whenever the browser lost the server
  (closed SSH tunnel), although LND was fine. Failures are now classified: lost
  connection -> "Unknown" plus a "not live" banner; 401 -> sign in again; only a 502
  (LND down) -> "Unreachable"; an unsynced node shows "Syncing".
- **Phase 8 test purchase done** (owner self-test, 1.006-sat fee): invoice settled, paid
  feed served and signed, ledger attributed it to `btc-fees` hourly. Not customer revenue.
- Listed on Glama (rated B) and awesome-mcp-servers PR #20232; MIT LICENSE added.

## 2026-11-19: machine discovery (CLAUDE_CODE_PROMPT_MACHINE_DISCOVERY.md)

- **Stale gate:** a paid feed past its stale limit answers 503 (`stale`, true
  `as_of`, free `sample_url`) and Aperture issues no invoice; refusals counted.
- **Prices:** hourly tokens USD-pegged at the live BTC rate: `btc-fees` $0.005
  (was 50 sats), `ln-network` $0.01 (was 100 sats); bundle unchanged (5,000 sats).
- **Discovery:** OpenAPI 3.1 (402 flow, live prices, response models, bundle
  paths), richer manifest and llms.txt (sources, licences, intervals, samples),
  `/.well-known/l402-services`, `/status(.json)`, `/robots.txt`; a test keeps them
  equal to the live routes.
- **Derived endpoints (free previews):** fee timing, area risk, energy snapshot,
  grid carbon intensity (`derived.py`).
- **MCP server** in `auragrid-mcp` 0.3.0 (not published): list_feeds, get_sample,
  get_quote, fetch (pays only within a configured cap).
- **Weekly self-check** timer, results on the dashboard.
- **Dashboard truth:** all-time invoice count (11,164), the invented "Mainnet
  Lightning Routing Liquidity" service removed, stale-refusal and sample-hit counters.
- **Listings:** 402 Index (2 endpoints, pending review); satring text updated.
  `LISTINGS.md`, `X402_PLAN.md` (decision pending).
- **Owner-delegated decisions:** hourly USD prices kept; derived endpoints stay free
  previews until the 90-day review (2027-02-17), since free calls are what get an
  unknown service tried; the EIA-based ones stay free regardless until EIA's
  commercial terms are confirmed. `auragrid-mcp` 0.3.0 built and checked for upload.
- **Contact** (owner-supplied) in the manifest, llms.txt and OpenAPI `info.contact`.
  `auragrid-mcp` 0.3.0 published on PyPI and the Official MCP Registry. `mcp-publisher` 1.8.9 installed; L402 Apps submission failed on their side (retry).

## 2026-11-11: Phase 4 (autonomous operations) and Phase 6 (report)

- **Listed on satring.com** (1,000-sat listing fee paid once, with the owner's
  approval). `langchain-auragrid` 0.2.0 rewritten for the current API.
- **Git history purged again:** the mock revenue database and a leaked password
  removed from every commit (force-push; all commit IDs changed).
- Dashboard: a free feed whose source stops publishing is an amber notice, not
  a red alert.

- **No charge for degraded data.** The hourly paid feeds (`btc-fees`,
  `ln-network`) are priced through the pricer (`dynamicprice`). While a feed is
  stale or missing, the pricer quotes 0 sats and Aperture serves it without
  payment; quotes, the manifest and responses say `degraded`. The 30-day
  bundle keeps its static price.
- **DVM under systemd** (`auragrid-dvm.service`, `Restart=always`, read-only
  sandbox). The tmux session and the `@reboot` cron entry are gone; the crontab
  is empty.
- **Log rotation** for `logs/*.log` and bitcoind's `debug.log`
  (`deploy/logrotate/auragrid`).
- **Operator dashboard**: alert banner, service and process health, feed
  freshness, TLS days left, disk, signing, and public traffic from Aperture's
  access log with a paywall funnel (`ops_monitor.py`, `/api/proxy/health`,
  `/api/proxy/traffic`). Still private (loopback + HTTP Basic).
- `verify_deployment.py` reports a degraded paid feed as WARN.
- **Fix: `btc-fees` refreshes every minute again.** Its `as_of` was the tip's
  block time, so the store kept one snapshot per block and the paid feed served
  data frozen at the last block (and marked it stale after 15 minutes without
  one). It now uses the observation time (`observed_at`).
- Freshness (stale, degraded, dashboard) is measured from the newest stored
  snapshot, not the last successful fetch: a fetch that returns unchanged data
  doesn't make a feed fresh. `energy/grid` currently shows stale because EIA's
  newest hour is about a day old.

## 2026-10-31: HTTPS, signing, EIA, docs

- HTTPS on :443 with a free Let's Encrypt IP certificate (6-day `shortlived`
  profile, renewed twice daily by `auragrid-cert-renew.timer`); :8080 retired.
- v2 response signing (`X-AurAgrid-Signature-V2`) with the production LND key
  through a `message:write`-only macaroon; the hardened client ships in
  `auragrid-mcp` 0.2.0.
- Live EIA v2 connectors (grid mix, SPR, LNG) and `auragrid-fetcher.service`:
  every upstream call happens in the fetcher, the API only reads stored
  snapshots.
- New catalog: 2 paid feeds + 1 bundle, 7 free feeds; old routes answer 410.
- Git history purged of leaked secrets (LND node folders, Nostr key, API key);
  affected keys and tokens revoked.

## 2026-10-30: Phases 0-2 (inspect, research, lean architecture)

- Research and source licensing in `RESEARCH.md`; only sources with clear
  redistribution terms are used.
- Removed everything that could move funds (sweep cron, liquidity sweeper,
  swarm payer) and the mock revenue database; 363 legacy files deleted
  (1.1 GB -> 80 MB).
- SQLite snapshot store with rolling retention (raw 7 days, daily forever),
  `provenance` on every response, free tier with per-IP rate limits, egress
  guard, NOAA marine weather feed, NIP-90 DVM answering only AurAgrid jobs.
- Dashboard password rotated.
