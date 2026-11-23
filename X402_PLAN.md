# x402 as a second payment rail: plan for Joshua (decision point 2)

Status (2026-11-23): **LIVE.** `/x402/v1/batch/btc-fees` ($0.002/request) and
`/x402/v1/batch/ln-network` ($0.004), USDC on Base (eip155:8453), paid straight to the owner's
address below. Facilitator: **PayAI** (`https://facilitator.payai.network`, no account or API key;
first 1,000 settlements free, then paid credits). Coinbase CDP was the first choice, but its sign-up
SMS code never reached the owner's Ugandan number; the CDP key-file path stays supported
(`AURAGRID_X402_CDP_KEY_FILE`). Fallback with no fee at all: Dexter (`https://x402.dexter.cash`), a
one-line change. A facilitator never holds funds: buyers sign a transfer to our address and it only
submits it. Stale feeds answer 503 before any payment is asked; settlement only after a 2xx; the
ledger keeps one row per transaction (`x402_payments`). No real payment has been made yet.
Library: `x402[evm]==2.27.0` (`requirements-x402.txt`; FastAPI deliberately not upgraded).

Receiving address (owner, 2026-11-23, self-custody, checksum verified, a plain wallet on Base):
`0x1e2c1E4BA923BD3612e43e4b82E22dD299626b6B` (also in `~/.auragrid/x402_pay_to`).

To switch it on (owner):
1. ~~A receiving address~~ done. A receiving address on Base you control (self-custody wallet, or a Coinbase account's Base
   USDC address). Public address only: never give the server a private key or seed.
2. A facilitator for **mainnet**: Coinbase CDP. Create a free account and a Secret API key at
   portal.cdp.coinbase.com, download its JSON and put it on the server as
   `~/.auragrid/cdp_api_key.json` (chmod 600; never paste it in chat). The rail reads it via
   `cdp-sdk` and uses `https://api.cdp.coinbase.com/platform/v2/x402`. (x402.org's free
   facilitator settles testnet only; the config refuses it on mainnet.)
3. Add to `~/.bashrc` (read by `scripts/run_backend.sh`), then restart `auragrid-api`:
   `export AURAGRID_X402_PAY_TO=0x1e2c1E4BA923BD3612e43e4b82E22dD299626b6B` and
   `export AURAGRID_X402_CDP_KEY_FILE=/home/ubuntu/.auragrid/cdp_api_key.json`
4. One real $0.002 test payment from a separate wallet, then the manifest and llms.txt advertise x402
   automatically, and the route can be listed in x402 Bazaar / x402scan / 402 Index.

## What x402 is, in one paragraph

x402 is the same idea as L402 (an HTTP 402 answer that says how to pay) but buyers pay in
stablecoins, usually USDC on Base, instead of Lightning. A "facilitator" checks the buyer's
signed payment and settles it on-chain to an address you choose. Coinbase runs the main
production facilitator (CDP). Agents built on Coinbase tooling pay x402, not Lightning, so it
reaches buyers L402 cannot.

## Costs and limits (CDP facilitator)

| Item | Value | Source |
|---|---|---|
| Facilitator fee | First 1,000 on-chain settlements per month free, then $0.001 each | CDP facilitator page |
| Verification | Always free; failed settlements are not billed | CDP FAQ |
| Gas | Paid by the facilitator (covered by the fee); buyers paying USDC need no gas token | CDP FAQ |
| Settlement time | About a second (P95 0.9 s on Base) | CDP FAQ |
| Networks | Base, Polygon, Arbitrum, Monad, World (EVM); Solana. **No Lightning** | CDP facilitator page |
| Minimum payment | Not documented | — |
| Batch settlement (sub-cent) | Exists in the protocol, but CDP's server SDK **withdrew** it (a flaw could accept payment without paying the receiver) | CDP FAQ, x402 batch docs |

Economics: a $0.005 call settled on-chain pays a $0.001 fee above 1,000 a month (20%).
Prices under about $0.01 only make sense inside the free 1,000, or with batch settlement once
it is safe again. AurAgrid's current volume is far below 1,000 a month, so the fee would be $0.

## Custody: who holds the money

- You give a **`payTo` address**. "Payments settle directly to the payTo address ... CDP never
  holds your revenue." Accepted: a self-custody wallet, Coinbase (retail, Business, Prime) or a
  CDP wallet.
- A CDP **API key** is still required: it lets the server talk to the facilitator, "it does
  not decide where funds land".
- One EVM address covers all EVM networks; Solana needs a separate address.
- Payments are final: no chargebacks; refunds are manual transfers. CDP screens every payment
  (OFAC, KYT) and declines flagged ones.

## Design: x402 next to L402 without touching the data routes

```
 buyer (L402)  --> Aperture :443  --> FastAPI /api/v1/...   (as today)
 buyer (x402)  --> Aperture :443  --> FastAPI /x402/v1/...  --> same handlers
                                       x402 middleware (verify + settle via CDP)
```

1. **Separate path prefix** `/x402/v1/<feed>`: Aperture routes it with `auth: off` to a small
   ASGI app mounted in the backend that runs the x402 `PaymentMiddlewareASGI`
   (`pip install "x402[evm,fastapi]"`), then calls the same feed handler. `/api/v1/` (L402)
   is untouched.
2. **Same stale gate first**: the x402 route checks `jobs.feed_degraded()` *before* asking for
   payment and answers 503 (`stale: true`, `as_of`) without a payment requirement, exactly like
   the L402 path. No one pays for stale data on either rail.
3. **Same price source**: the x402 price is the pricer's USD target (`pricing.yaml endpoints:`),
   so both rails quote the same dollars.
4. **Same ledger**: add a `rail` column (`l402` | `x402`) to `token_usage`, keyed by the L402
   payment hash or the x402 settlement transaction hash; the dashboard splits revenue by rail.
5. **Discovery**: x402 Bazaar lists a route only after a successful paid settlement through
   CDP; metadata (description up to 500 characters) goes in the route config. Also list in
   x402scan, agentic.market and 402 Index (which accepts x402 registrations).

Estimated work once decided: about a day, plus a real $0.01 test payment from your own wallet.

## What you must decide

1. **Yes or no to x402.** It adds reach (Coinbase-tooling agents) at the cost of a second
   account and a stablecoin receiving address.
2. **Receiving address**: self-custody (you hold the key; you handle conversion to fiat) or a
   Coinbase account (convenient off-ramp; Coinbase holds the funds and requires KYC).
3. **A CDP account and API key** in your name (free). Keys would be stored outside the repo
   like the other secrets, never in this file.
4. **Prices** for x402 (suggest the same USD as L402: $0.005 and $0.01 per hour token; note
   x402 "exact" charges per request, so an hour pass is not native — per-request $0.001-0.002
   would match).

## Risks and questions for you to check

- **Custody and loss**: a self-custody key lost = funds lost; an exchange account can be frozen.
- **Volatility**: USDC is designed to hold $1 but depends on its issuer (Circle); other tokens
  are volatile.
- **Tax and regulation**: none of the docs give guidance. Receiving stablecoins is income in most
  jurisdictions, and depending on where you live, running a payments-accepting service may have
  reporting duties. Check with an accountant before going live.
- **Compliance declines**: CDP may decline some buyers (KYT); nothing to do on our side.
- **Facilitator dependency**: settlement depends on CDP's service; the public x402.org
  facilitator is testnet only.

## Not recommended now

- **Alby 402-proxy** (L402 + x402 + MPP in one proxy): its README says "For Testing Only";
  it passes the wallet connection string in every request and keeps replay protection in memory.
- **MPP over Lightning** (Spark SDK): session payments "coming soon"; third-party SDK; we
  already sell Lightning through Aperture.

## Sources read (2026-11-19)

x402.org; github.com/coinbase/x402; docs.cdp.coinbase.com/x402/ (welcome, quickstart-for-sellers,
seller/facilitator, seller/production-configuration, seller/get-discovered, support/faq);
docs.x402.org (network-and-token-support, facilitator, schemes/batch-settlement,
dev-tools/facilitators); github.com/getAlby/402-proxy; mpp.dev (index, payment-methods/lightning,
faq).
