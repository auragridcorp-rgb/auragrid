# AurAgrid - The Blueprint
A Comprehensive Guide to a Self-Sovereign, Agentic Energy Data Network
Author: Joshua Sunge

## System Architecture & Component Mapping
- **Bitcoin Core (`auragrid-bitcoind`):** Pruned Mainnet node (5GB limit) running on Docker (`ruimarinho/bitcoin-core:24.0.1`).
- **Node 1 (`auragrid-lnd`):** Swarm Agent spending wallet (`lightninglabs/lnd:v0.18.0-beta`).
- **Node 2 (`auragrid-lnd-2`):** Service provider revenue-generating wallet.
- **API Backend (`auragrid_api`):** Python FastAPI gateway validating L402 proofs of payment.
- **Swarm Agents (`auragrid_swarm`):** Autonomous Python bots executing micropayments via Lightning.

## Operational Workflow & Economic Model
- **L402 Standard:** Machine-to-machine monetization bypassing traditional banking rails.
- **Fiat Off-Ramp:** Self-custodial sats routed through Mt. Pelerin Bridge Wallet to liquidate directly into a global USD bank account.
- **Revenue Target:** $1,000 in the first month dedicated entirely to Oracle Cloud block storage expansion.
