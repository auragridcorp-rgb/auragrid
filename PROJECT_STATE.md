# AURAGRID SAVE STATE - INITIALIZE CONTEXT

"Hey! We are working on the AurAgrid project—a Lightning-monetized M2M API using Bitcoin Core, LND, FastAPI, Aperture (L402 proxy), and FastMCP. Please read ~/AurAgrid/PROJECT_STATE.md to catch up on our architecture, network rules, and past milestones before we write any code."

## Current System State
1. **Hardware & OS**: High-performance OCI ARM instance `auragrid-mainnet` (4 Ampere OCPUs, 24GB RAM, Ubuntu) at Public IP `84.12.79.124`.
2. **Environment & Core Backend**: Active Conda environment `auragrid_env` with core stack (`asyncpg`, `httpx`, `fastapi`, `uvicorn`, `fastmcp`, `websockets`, `nostr`, `secp256k1`). Production telemetry routes live on port `8000` supporting an 18-node simulation catalog.
3. **L402 Payment Proxy (Aperture)**: Verified on port `8080`. Intercepts production routes returning HTTP 402 with mainnet Lightning invoices. **Verified locally:** Paywall successfully intercepts requests and generates live BOLT11 invoices.
4. **Bitcoin Core & LND Node**: Mainnet daemon synced. **Liquidity secured:** 5.3M sat inbound channel active via Amboss Magma. **Automated Treasury:** Background `cron` job (`sweep_to_vault.py`) runs every 6 hours, maintaining a 1M sat routing buffer and sweeping profits directly to a self-custodial Kraken Taproot wallet.
5. **M2M & AI Discovery Suite**:
   - `AurAgrid/auragrid_sdk/mcp_server.py`: Fully implements `FastMCP` exposing 12 active retail and wholesale tool definitions for native AI agent calls.
   - `AurAgrid/auragrid_sdk/client.py`: Handles local client calls with strict L402 error checks.
   - `AurAgrid/dvm/nip90_worker.py`: **Live in Production**. Dual-Threat Nostr Worker running 24/7 in a detached `tmux` session (`auragrid_worker`), actively hunting NIP-90 jobs and routing them to the paywall.
6. **Network & Public IP Config**: `EXTERNAL_PAYWALL` explicitly bound to `http://84.12.79.124:8080/api/v1`. Network port `9735` is fully open and globally routing.

## Immediate Next Steps
1. **Awaiting Directive**: Core infrastructure, inbound liquidity, and automated treasury are 100% operational. Ready to pivot to AI agent workflows or API expansion based on the next command.
