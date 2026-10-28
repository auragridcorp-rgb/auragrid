# AURAGRID SAVE STATE - INSTITUTIONAL M2M ORACLE

"We are scaling AurAgrid as a professional, institutional-grade M2M telemetry oracle. Our focus is absolute technical dominance: L402 Lightning settlement, NIP-89/90 Nostr discovery, FastMCP agent integration, and zero-friction developer documentation."

## Current System State
1. **Hardware & OS**: High-performance OCI ARM instance `auragrid-mainnet` (4 Ampere OCPUs, 24GB RAM, Ubuntu) at Public IP `84.12.79.124`.
2. **Environment & Core Backend**: Active Conda environment `auragrid_env` with core stack (`asyncpg`, `httpx`, `fastapi`, `uvicorn`, `fastmcp`, `websockets`, `nostr`, `secp256k1`, `grpcio`, `pyyaml`). Production telemetry routes live on port `8000` supporting a 22-endpoint institutional catalog.
3. **L402 Payment Proxy & Settlement**: Mainnet LND node active with ~6.3M sats inbound liquidity capacity and ~29k sats in settled revenue. Aperture/FastAPI payment gating fully operational, issuing live BOLT11 invoices and cryptographic macaroons.
4. **Autonomous Discovery & Swarm Integration**:
   - **NIP-89 DVM Broadcast Matrix**: Successfully propagated Kind 31990 capability advertisements across global relays (`relay.damus.io`, `nos.lol`) for core telemetry streams (ESG Carbon, ERCOT LMP, Maritime Congestion).
   - **Machine Discovery Layer**: `llms.txt` and unified `agent-manifest.json` fully standardized for automated agent framework indexing (LangChain, CrewAI, AutoGPT).
   - **Background Workers**: Dual-threat Nostr and volatility workers running 24/7 in detached `tmux` sessions (`auragrid_worker`), alongside automated treasury sweeps (`dynamic_liquidity_sweep.py`) securing profits to a Kraken Taproot vault (`bc1p...`).
5. **Dynamic Pricing & Revenue Attribution Architecture**:
   - **gRPC Pricer (`pricer.py`)**: Multi-feed aggregated BTC/USD engine (Coinbase, Kraken, Mempool.space) running on port `10010` to power Aperture's real-time USD-pegged satoshi pricing.
   - **Request-Time Ledger (`l402_ledger.py`)**: SQLite WAL middleware capturing sha256 preimage-to-endpoint mappings on successful paid requests for exact revenue attribution.
   - **Command Center Dashboard (`main.py`)**: Secure FastAPI operator interface with Tailwind UI and HTTP Basic Auth (`Sungey`/`***REMOVED***`), delivering live channel stats and revenue breakdown analytics.

## Recent Milestones
* Resolved websocket handshake and f-string syntax bottlenecks in `nip89_broadcaster.py`, achieving successful relay propagation.
* Overhauled repository documentation (`README.md` and `agent-manifest.json`) to project institutional-grade authority without exposing raw management control interfaces.
* Seeded infrastructure integration proposals targeting core multi-agent framework repositories.
* Successfully deployed and verified the production gRPC dynamic pricer and request-time L402 ledger middleware.

## Immediate Next Steps
1. Monitor incoming L402 payment challenges and swarm probe traffic via the backend logs and command center dashboard.
