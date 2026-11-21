# AurAgrid listings

Where AurAgrid is listed, the exact text submitted, its status, and how to edit or
withdraw it. Checked weekly by `selfcheck.py` (listings section). Rules: free listings only,
truthful text from verified facts, nothing posted to communities without Joshua's approval.

| Platform | What | Date | Status | Edit / withdraw |
|---|---|---|---|---|
| [satring.com](https://satring.com/services/auragrid-bitcoin-fees-lightning-network-data) | `btc-fees` (+ ln-network, bundle in the text) | listed 2026-11-11, text and price updated 2026-11-19 | **Live** | `PATCH` / `DELETE https://satring.com/api/v1/services/auragrid-bitcoin-fees-lightning-network-data` with header `X-Edit-Token: $(cat ~/.auragrid/satring_edit_token)`; free |
| [402 Index](https://402index.io) | `btc-fees` (id `22131ab3-de4b-42c2-b58e-f1f5852ca889`) | 2026-11-19 | **Pending review** (L402 challenge verified) | Edit/delete needs a domain claim, which rejects bare IPs: email hello@402index.io |
| [402 Index](https://402index.io) | `ln-network` (id `ad2d6d6e-ea85-4e8b-8267-b3fc3e107c06`) | 2026-11-19 | **Pending review** (L402 challenge verified) | As above |
| [L402 Apps](https://www.l402apps.com) | `btc-fees`, `ln-network` | tried 2026-11-19 | **Retry later: their side** | `POST /api/api-submissions` {url, invoice, description}. Joshua approved 10-sat receive-only invoices. Every valid invoice was rejected ("Could not decode your Lightning invoice"), and their own `/api/apis` answered "Failed to create Lightning invoice" at the same time, so their Lightning backend looks down. Retry with a fresh invoice (the old ones expire unpaid, harmlessly) |
| [PyPI](https://pypi.org/project/auragrid-mcp/0.3.0/) | `auragrid-mcp` 0.3.0 | 2026-11-19 | **Live** | Project-scoped token (Joshua); yank a release from the PyPI project page |
| [Official MCP Registry](https://registry.modelcontextprotocol.io/v0/servers?search=io.github.auragridcorp-rgb/auragrid) | `io.github.auragridcorp-rgb/auragrid` 0.3.0 | 2026-11-19 | **Live** | `mcp-publisher publish` with a new version in server.json (login: GitHub device flow as auragridcorp-rgb); status changes via `mcp-publisher status` |
| PulseMCP | — | — | Automatic | Copies the Official MCP Registry (its own submissions are paused) |
| [Glama](https://glama.ai/mcp/servers/auragridcorp-rgb/auragrid) | `auragridcorp-rgb/auragrid` | 2026-11-21 (first try rejected: no LICENSE; MIT added) | **Listed, rated B**; not in search until claimed + Dockerfile checks pass | Claim via the server page's admin settings (GitHub login), then paste `auragrid_mcp/Dockerfile` in its admin page |
| Smithery | — | — | Not possible | Stopped accepting listings (now part of Arcade) |
| [punkpeye/awesome-mcp-servers](https://github.com/punkpeye/awesome-mcp-servers/pull/20232) | PR #20232, Finance & Fintech | 2026-11-21 | **Open** (1 line, mergeable; Glama badge shows B; title has 🤖🤖🤖) | Fork `auragridcorp-rgb/awesome-mcp-servers`; the server's SSH key can push to it. Awaiting maintainer merge |
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

Contact: Joshua gave auragridcorp@gmail.com on 2026-11-19 (`AURAGRID_CONTACT` in ~/.bashrc); it
appears in the manifest, llms.txt and OpenAPI `info.contact`. Listings submitted before that
date have none.

## Drafts (not submitted)

**punkpeye/awesome-mcp-servers**, section "Finance & Fintech" (one line, alphabetical; PR title
ends with 🤖🤖🤖 because an agent prepared it):

```markdown
- [auragridcorp-rgb/auragrid](https://github.com/auragridcorp-rgb/auragrid) 🐍 ☁️ - Bitcoin fee and Lightning network data from AurAgrid's own nodes (L402, free delayed samples) and free U.S. EIA, USGS and NOAA feeds; responses signed by the node key.
```

**Community posts.** Standing permission from Joshua (2026-11-21) to post when well researched
and appropriate, never spam. Plan: one post, only after the first real purchase is verified
end to end (a broken flow in front of buyers would cost more than silence). Best venue:
Stacker News ~bitcoin (L402 audience); needs Joshua's account, since the server has no login there.

**Stacker News / Nostr** draft:

> I run AurAgrid, a small L402 data service on my own Bitcoin and Lightning nodes: fee
> estimates every minute ($0.005 per 1-hour token) and Lightning network stats hourly ($0.01),
> with free delayed samples and signed responses. Stale data answers 503 instead of taking
> payment. Manifest: https://84.12.79.124/agent-manifest.json. Feedback welcome.
