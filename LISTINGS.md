# AurAgrid listings

Where AurAgrid is listed, the exact text submitted, its status, and how to edit or
withdraw it. Checked weekly by `selfcheck.py` (listings section). Rules: free listings only,
truthful text from verified facts, nothing posted to communities without Joshua's approval.

| Platform | What | Date | Status | Edit / withdraw |
|---|---|---|---|---|
| [satring.com](https://satring.com/services/auragrid-bitcoin-fees-lightning-network-data) | `btc-fees` (+ ln-network, bundle in the text) | listed 2026-11-11, text and price updated 2026-11-19 | **Live** | `PATCH` / `DELETE https://satring.com/api/v1/services/auragrid-bitcoin-fees-lightning-network-data` with header `X-Edit-Token: $(cat ~/.auragrid/satring_edit_token)`; free |
| [402 Index](https://402index.io) | `btc-fees` (id `22131ab3-de4b-42c2-b58e-f1f5852ca889`) | 2026-11-19 | **Pending review** (L402 challenge verified) | Edit/delete needs a domain claim, which rejects bare IPs: email hello@402index.io |
| [402 Index](https://402index.io) | `ln-network` (id `ad2d6d6e-ea85-4e8b-8267-b3fc3e107c06`) | 2026-11-19 | **Pending review** (L402 challenge verified) | As above |
| [L402 Apps](https://www.l402apps.com) | `btc-fees`, `ln-network` | — | **Blocked: Joshua** | Their API requires a payout target for its 10-sat submission reward: a 10-sat invoice from our node (receiving only) or a Lightning address. Creating invoices on the node was not allowed without your approval |
| Official MCP Registry | `io.github.auragridcorp-rgb/auragrid` | — | **Blocked: Joshua** | Needs `auragrid-mcp` 0.3.0 on PyPI, the repo public and `mcp-publisher login github` as you: `auragrid_mcp/PUBLISHING.md` |
| PulseMCP | — | — | Automatic | Copies the Official MCP Registry (its own submissions are paused) |
| Glama | — | — | After the awesome-mcp-servers PR | Syncs with punkpeye/awesome-mcp-servers; or "Add Server" with your login |
| Smithery | — | — | Not possible | Stopped accepting listings (now part of Arcade) |
| punkpeye/awesome-mcp-servers | PR | — | **Draft: Joshua** | Needs a fork + PR from your GitHub account (the server's SSH key cannot fork), and the repo public first. Text below |
| x402 Bazaar / agentic.market | — | — | Waits for decision point 2 | Only once x402 exists (`X402_PLAN.md`) |
| LangChain | `langchain-auragrid` 0.2.0 | 2026-11-11 | Public on GitHub | Not on PyPI; publish needs your token |

## Standard description (one line, verified facts)

> AurAgrid: Bitcoin fee estimates and mempool stats from its own Bitcoin Core node every minute,
> and Lightning network statistics from its own LND node hourly, sold over L402 as 1-hour tokens
> (USD-pegged, $0.005 and $0.01) with free delayed samples; stale data answers 503 and is never
> charged. Free: U.S. EIA grid mix, SPR and LNG exports, USGS earthquakes, NOAA marine weather,
> and derived fee-timing, grid-carbon and area-risk endpoints. Signed responses with provenance.

## Exact text submitted

**satring (2026-11-19 update)**, `pricing_sats: 8`, model `flat`, protocol L402, categories data + finance:

> Bitcoin fee estimates and mempool stats from AurAgrid's own Bitcoin Core node, observed every
> minute (L402 1-hour token, USD-pegged at $0.005, about 8 sats). Lightning network size, capacity
> and fee percentiles from its own LND node, hourly (/api/v1/batch/ln-network, $0.01, about 16
> sats). 30-day bundle of both: 5,000 sats. Free samples: /api/v1/batch/btc-fees-delayed and
> /api/v1/batch/ln-network-daily. Stale data is never charged: a paid feed past its freshness
> limit answers 503 and issues no invoice. Every response carries provenance and an LND-signed v2
> signature. Also free: U.S. EIA grid mix, SPR and LNG exports, USGS earthquakes, NOAA marine
> weather, and derived fee-timing, grid-carbon and area-risk previews. Manifest:
> https://84.12.79.124/agent-manifest.json Status: https://84.12.79.124/status.json

**402 Index, btc-fees** (`category: bitcoin`, `provider: AurAgrid`, `price_sats: 8`, `price_usd: 0.005`, GET):

> Bitcoin fee estimates (1-144 block targets), mempool size and minimum fee, and the last block's
> fee percentiles, observed every minute from AurAgrid's own Bitcoin Core node. L402 1-hour token,
> USD-pegged at $0.005 (about 8 sats). Free 10-minute-delayed sample: /api/v1/batch/btc-fees-delayed.
> Signed responses with provenance; stale data returns 503 and is never charged.

**402 Index, ln-network** (`category: lightning`, `provider: AurAgrid`, `price_sats: 16`, `price_usd: 0.01`, GET):

> Lightning Network size, capacity, channel-size and fee percentiles from AurAgrid's own LND
> node's gossip graph, refreshed hourly. L402 1-hour token, USD-pegged at $0.01 (about 16 sats).
> Free previous-day sample: /api/v1/batch/ln-network-daily. Signed responses with provenance;
> stale data returns 503 and is never charged.

No contact address was submitted anywhere: it is optional, and the prompt says to use the one
Joshua gives.

## Drafts (not submitted)

**punkpeye/awesome-mcp-servers**, section "Finance & Fintech" (one line, alphabetical; PR title
ends with 🤖🤖🤖 because an agent prepared it):

    - [auragridcorp-rgb/auragrid](https://github.com/auragridcorp-rgb/auragrid) 🐍 ☁️ - Bitcoin fee and Lightning network data from AurAgrid's own nodes (L402, free delayed samples) and free U.S. EIA, USGS and NOAA feeds; responses signed by the node key.

**Stacker News / Nostr** (only if you approve posting it, from your own account):

> I run AurAgrid, a small L402 data service on my own Bitcoin and Lightning nodes: fee
> estimates every minute ($0.005 per 1-hour token) and Lightning network stats hourly ($0.01),
> with free delayed samples and signed responses. Stale data answers 503 instead of taking
> payment. Manifest: https://84.12.79.124/agent-manifest.json. Feedback welcome.
