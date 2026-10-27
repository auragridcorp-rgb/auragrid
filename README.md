<div align="center">

# ⚡ AurAgrid Autonomous Telemetry Oracle
### Sovereign Machine-to-Machine (M2M) Data Marketplace & L402 Settlement Layer

[![Mainnet Status](https://img.shields.io/badge/Mainnet-Operational-success?style=for-the-badge&logo=lightning)](http://84.12.79.124:8000/)
[![Protocol](https://img.shields.io/badge/Protocol-L402%20%2F%20Lightning-orange?style=for-the-badge&logo=bitcoin)](https://lightning.network/)
[![Nostr DVM](https://img.shields.io/badge/Nostr-NIP--89%2F90-purple?style=for-the-badge&logo=nostr)](https://nostr.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

</div>

---

## 🌐 Overview
**AurAgrid** ([Command Center](http://84.12.79.124:8000/)) is a sovereign telemetry oracle built for autonomous AI agent swarms, algorithmic trading bots, and enterprise pipelines. By combining **Lightning Network L402 micro-payments**, **Nostr Data Vending Machines (DVMs)**, and drop-in **LangChain tool wrappers**, AurAgrid standardizes real-time data discovery and instant programmatic monetization without API keys or human intervention.

---

## 🚀 Core Architecture & Features
* **L402 Lightning Native:** Automatically handles HTTP 402 payment challenges via LND mainnet invoices, settling micro-transactions instantly.
* **Cryptographic Attestation:** Validates `X-AurAgrid-Signature` headers signed directly by the oracle node.
* **Autonomous Discovery:** Broadcasts capabilities globally across Nostr relays via NIP-89/90 and machine-readable `llms.txt` registries.
* **Affiliate Attribution:** Passes `orchestrator_pubkey` headers to route automated revenue splits upon invoice settlement.

---

## 🛠️ Quick Installation & Usage

### Python / LangChain Integration
```bash
pip install langchain-auragrid
