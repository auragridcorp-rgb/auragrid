# AurAgrid

A small data oracle for software agents, paid per hour over Lightning (L402).

- **Paid:** Bitcoin fee/mempool data and Lightning Network statistics, computed
  by AurAgrid's own Bitcoin Core and LND nodes.
- **Free:** delayed copies of those two, plus U.S. public-domain feeds: EIA grid
  demand and generation mix, Strategic Petroleum Reserve stocks, LNG exports,
  NOAA marine weather and USGS earthquakes.

Every response carries `provenance` (source, terms, attribution, `as_of`,
`retrieved_at`) and a `stale` flag. There is no simulated or sample data: a feed
that has no fresh enough data answers `503`.

## Endpoints

Base URL: `https://84.12.79.124`. The live list, with prices and status, is
always at [`/agent-manifest.json`](https://84.12.79.124/agent-manifest.json).

| Endpoint | Access | Data |
|---|---|---|
| `/api/v1/batch/btc-fees` | 50 sats / hour | Fee estimates for 1-144 blocks, mempool size and min fee, last-block fee percentiles (1 min) |
| `/api/v1/batch/ln-network` | 100 sats / hour | Nodes, channels, capacity, channel-size and fee percentiles (hourly) |
| `/api/v1/bundle/btc-fees`, `/api/v1/bundle/ln-network` | 5,000 sats / 30 days | Both paid feeds on one token |
| `/api/v1/batch/btc-fees-delayed` | free | `btc-fees`, at least 10 minutes old |
| `/api/v1/batch/ln-network-daily` | free | Previous UTC day's final `ln-network` snapshot |
| `/api/v1/energy/grid` | free | Hourly U.S. demand and generation by fuel for the Lower 48, EIA regions and balancing authorities (EIA-930) |
| `/api/v1/batch/spr-stockpile` | free | Weekly SPR and commercial crude stocks (EIA) |
| `/api/v1/batch/lng-supply-chain` | free | Monthly U.S. LNG exports by terminal and destination (EIA) |
| `/api/v1/batch/earthquakes` | free | M2.5+ past 24 h, M4.5+ past 7 days (USGS) |
| `/api/v1/batch/maritime-weather` | free | Latest NOAA NDBC buoy and coastal station observations |

Free endpoints need no account and are limited to 30 requests per minute per IP.
Terms: [`/terms`](https://84.12.79.124/terms).

```bash
curl -s --compressed https://84.12.79.124/api/v1/energy/grid | jq .data.lower48
```

## Verifying responses

Successful data responses are signed by the oracle's Lightning node key
(`038041ef6643bb59d9ec73568a952183b261683968cb4000b547302fa464cf8228`):
`X-AurAgrid-Signature-V2` covers the method, path, a SHA-256 of the body, the
`X-AurAgrid-Timestamp` and your optional `X-AurAgrid-Nonce`. The scheme and a
verifier are in `attestation.py`; `client.py` and the MCP server use it when you
pin the key (`verify_signature=attestation.make_verifier(PUBKEY)` /
`AURAGRID_SIGNING_PUBKEY`). Pin the key from a trusted source, not from a response.

## Paying (L402)

A paid endpoint answers `402` with `WWW-Authenticate: L402 macaroon="…", invoice="…"`.
Pay the invoice, then repeat the request with `Authorization: L402 <macaroon>:<preimage>`.
The token is valid for an hour (30 days for the bundle), so reuse it.

`client.py` does this safely against your own LND node: it requires a per-payment
cap, supports a session budget, and checks the invoice against the free price
quote and the preimage against the payment hash.

```bash
export AURAGRID_URL=https://84.12.79.124
export LND_REST_URL=https://127.0.0.1:8084 LND_TLS_CERT=~/.lnd/tls.cert LND_MACAROON_PATH=~/agent.macaroon
export AURAGRID_MAX_SATS=200
python client.py /api/v1/batch/btc-fees
```

The oracle serves HTTPS only, with a Let's Encrypt certificate for the IP address
(renewed automatically; no domain). The client refuses to send L402 tokens (bearer
credentials) over plain http to anything but loopback.

## MCP server

`auragrid_sdk/mcp_server.py` exposes one FastMCP tool per endpoint. The free tools
work without any setup. The paid tools pay through `client.py` only when
`AURAGRID_MAX_SATS` and the LND variables above are set.

```bash
fastmcp run auragrid_sdk/mcp_server.py
```

## How it runs

```
upstream sources ──> fetcher.py ──> var/feeds.db ──> FastAPI (127.0.0.1:8000) ──> Aperture L402 (HTTPS :443)
(EIA, NOAA, USGS,     (schedule,     (SQLite snapshot   (reads snapshots only)      (prices, free-tier
 bitcoind, LND)        retries)       store)                                          rate limits)
```

- `fetcher.py` and `jobs.py`: what is fetched, how often, freshness limits.
  A failed fetch never overwrites the last good snapshot.
- `app/main.py`: the API, catalog, manifest, terms and retired routes (410).
- `pricing.yaml` (`access:`) and `aperture.base.yaml`: which feeds are free, paid
  or retired, and paid prices. `gen_aperture.py` generates Aperture's config from them.
- `deploy/`: systemd units and setup steps.
- `RESEARCH.md`: sources, licensing terms, demand and pricing rationale.

## Development

```bash
/home/ubuntu/miniconda/envs/auragrid_env/bin/python -m unittest discover -s tests -t .
```

The tests are offline. Live checks: `python eia_grid_feed.py --check` (needs
`EIA_API_KEY`) and `python fetcher.py --once`.

## Data sources and attribution

- U.S. Energy Information Administration (EIA): public domain. EIA does not endorse AurAgrid.
- NOAA National Data Buoy Center: public domain. NOAA does not endorse AurAgrid.
- U.S. Geological Survey: public domain. Credit: U.S. Geological Survey.
- Bitcoin and Lightning data: computed by AurAgrid's own nodes from public network data.
