# Prompt for Claude Code: make AurAgrid discoverable and sellable by machines

Run `claude` inside `~/AurAgrid` on the server and paste everything below the line.

---

You are improving AurAgrid for its owner, Joshua. The service is healthy but has zero paying customers and almost no discovery. The goal is a one-time setup that lets machine buyers (AI agents and the directories they query) find, understand, try and pay for the data without Joshua doing daily outreach. Revenue expectations are small and uncertain. The cost ceiling is 0 USD/month of new spending.

Work in phases and report each in a few lines. Investigate the server yourself. Stop at the marked decision points.

## Hard rules
1. Never print secrets (macaroons, keys, env files, passwords, API keys). Report present/absent and paths only.
2. Never spend or move money. No lncli payments, channel changes, paid signups or paid API plans. Read-only `lncli` is fine.
3. **Listings: authorized, within limits.** Joshua has given you full authority to create listings and submissions for AurAgrid on any directory, registry, marketplace, awesome-list or similar catalog you judge appropriate, in whatever way is available (web forms, APIs, GitHub pull requests, registry CLIs). Limits that always apply:
   - Content is truthful and uses only facts you verified from the running system. No customer, revenue or "real-time" claims the data can't back up.
   - Listings are free. If a listing costs money (for example Satring charges about 1,000 sats), show Joshua the amount and the invoice or payment page and let him pay it himself from his own wallet. Never pay from the LND node. Total listing fees across everything must not exceed 5,000 sats without Joshua's approval.
   - Never type, store or guess passwords, 2FA or verification codes, and never bypass CAPTCHAs. When a login or code is needed, stop and ask Joshua to complete that step, then continue. Use the contact address Joshua gives you in the terminal (ask once); do not read it from anywhere else.
   - One honest account per platform, in Joshua's name as AurAgrid's creator. No fake accounts, ratings, reviews or self-promotion in comment threads; follow each platform's rules and rate limits.
   - Accepting standard listing terms is fine, but summarize them for Joshua first, and stop and ask if the terms involve payment, exclusivity, transfer of rights, or sharing data beyond the listing itself.
   - Unsolicited DMs, emails and posts in communities (Stacker News, Nostr, forums) stay as drafts unless Joshua approves each one.
   - Keep `LISTINGS.md`: platform, URL, date, exact text submitted, status, and how to edit or withdraw it.
4. Back up first (`cp -a ~/AurAgrid ~/AurAgrid.bak-$(date +%Y%m%d-%H%M)`), keep `CHANGELOG.md`, and ask before restarting Aperture, LND, the pricer or the API.
5. No simulated data, and no claim the data cannot back up. Every feed states its real source and update interval, and stale data is labelled stale.
6. Never take payment for stale data (see Phase 1).
7. Nothing automated may edit code, change prices, install packages or spend.

## Phase 0: inspect (read-only; report first)
- Current routes, Aperture services and prices, the discovery files that exist (`/openapi.json`, `/llms.txt`, `/agent-manifest.json`, `/.well-known/*`), the free sample routes, and what `langchain-auragrid` and the Nostr DVM worker currently expose.
- Pull the last 30 days of Aperture log entries for the repeating client `203.161.57.177` (user agent `python-httpx`, about every 6 hours on `/api/v1/batch/btc-fees`). Report what it requests and whether it ever follows up after the 402. Do a reverse DNS / whois lookup and say what it probably is. Do not contact it.
- Confirm what is actually free today. The dashboard shows 0 free requests: find out whether free routes exist, are reachable through Aperture, and are linked from the 402 response.
- Look at why the dashboard shows 62 invoices in the funnel but 5,000 in the revenue panel (likely a query cap) and report the cause.

## Phase 1: fix the basics that agents depend on
1. **Stale gate.** `energy/grid` has been stale for about 9.8 hours (the source stopped publishing; your stale limit is 6 hours). Make every paid route refuse to issue an invoice, and return 503 with `stale: true` and the true `as_of`, once its data is past that feed's stale limit. Add a test.
2. **Free sample on every feed.** Each paid feed has a free delayed sample (real data, truncated lists, true `as_of`). Reuse `samples.py` if present. Link it from the 402 response body (a `sample_url` field) and from every discovery file.
3. **Prices.** Research found that real agent payments are sub-cent to a few cents per call (about $0.001 to $0.01), that agents often cap spending near $0.10 per call, that the best-selling categories are boring utilities (live gas prices across chains had 21 paying wallets; grid carbon intensity had 5), and that one seller who cut a price to $0.001 still got zero payments because most agents have no funded wallet. So price is not the main lever; avoid being absurdly high and stay cheap. Note that Aperture charges per token (a time pass), so the "per call" figure is per pass window. Propose this ladder, adjusting after you inspect the actual routes and the live BTC/USD rate (the pricer has it; do not hard-code a rate):
   - Simple feeds (`btc-fees`, `earthquakes`, `ln-network`, `maritime-weather`): about $0.005 to $0.02 per short pass (a few minutes to one hour). The current 50 sats is roughly $0.04 at about $77,000/BTC, which is 4 to 8 times the sub-cent norm; `btc-fees` is the best-evidenced category, so make it the cheapest and the hero.
   - Derived and combined endpoints (Phase 4): about $0.02 to $0.10 per pass.
   - 30-day pass: about $3 to $10 until there is evidence of buyers. The current $49 has no evidence behind it. This is judgment, not researched fact, so label it that way.
   - Keep every paid price below $0.10 per pass and every pass below about $10.
   - The 5,000-sat, 19,300-sat, 63,200-sat and 128,000-sat invoices on the dashboard (including the "Mainnet Lightning Routing Liquidity" service) look far above any agent spending cap. Find out what they are, report whether any evidence supports demand for them, and propose whether to keep, reprice or remove them.
   - Keep a free delayed sample on every feed. For any future x402 rail, note the facilitator economics: the Coinbase facilitator is free for the first 1,000 onchain transactions per month and then costs $0.001 each, so exact-price tickets below about $0.01 should use batch settlement (see Phase 5).
4. **Dashboard truth.** Fix the invoice-count discrepancy so the funnel and revenue panel agree, and add "stale refusals" and "free sample hits" counters.

**Decision point 1:** show Joshua the proposed price table (feed, current price, proposed price, USD at the current BTC rate, evidence or reason) and wait for approval before changing Aperture config.

## Phase 2: machine-readable discovery that matches reality
Generate all of these from the live route table so they can never drift. Add a test that fails if a documented path does not exist, or a live path is undocumented.
- `/openapi.json`: every path, parameters, response schema, the `402` response with a description of the L402 flow, the price in sats and USD, and the sample URL. Validate it with an OpenAPI validator.
- `/llms.txt` and `/agent-manifest.json`: feed names, sources and licences, update intervals, free vs paid, price, sample URL, signing public key, contact, terms URL, and the status URL. The endpoint count must match reality.
- `/.well-known/l402.json` (or whatever file the Satring and 402 Index docs call for; fetch their current docs, do not rely on memory).
- A `/status` page and `/status.json` showing each feed's freshness and the service uptime. Directories health-check sellers, so honest uptime is part of the product.
- `robots.txt` that allows the discovery files and API docs.

## Phase 3: MCP server
Build a small MCP server (stdio and, if simple, streamable HTTP) wrapping the existing `client.py`/`langchain-auragrid` code:
- Tools: `list_feeds`, `get_sample(feed)`, `get_quote(feed)`, `fetch(feed)` (pays via L402).
- `fetch` must enforce a mandatory per-payment cap and an optional budget, checking the quote before paying, using the hardened client. No default wallet; the user supplies wallet configuration.
- Package it with a README, `server.json` (or whatever the Official MCP Registry currently requires; fetch its docs), and a minimal install command. Tests with a fake Aperture.
- Do not publish to PyPI/npm/registries. Prepare the package and list the publish steps for Joshua.

## Phase 4: derived products (the actual value)
Raw public data is free elsewhere, so add 2 to 3 derived endpoints built only from feeds already running and clearly marked `method: "derived"` with the formula in `provenance.derivation`:
- A **fee-timing signal** from `btc-fees` history: recent percentile of fee rates, trend, and the lowest-fee window in the last 24 hours and 7 days. No predictions presented as certain.
- **Seismic or marine risk flags** that combine `earthquakes` and `maritime-weather` for configurable areas: only counts and measured values, no forecasts.
- A **grid and energy snapshot** that combines `energy/grid`, `spr-stockpile` and `lng-supply-chain` into one JSON, with each part's own `as_of` and stale flag.
- **Grid carbon intensity** (priority, because grid carbon intensity had real paying wallets in the research): gCO2/kWh derived from the generation mix in `energy/grid` and a published emission-factor table. Name and link the table and its licence in `provenance`. Check the grid data source's terms first: for example, EIA's API terms don't explicitly address commercial use, require crediting EIA, forbid implying endorsement, forbid using its logo, and forbid modifying content while still claiming EIA as the source. Label derived values as AurAgrid-derived and credit the source. Report what you find and ask Joshua before selling anything whose terms are unclear.
- Also evaluate (research only, report back) two or three "boring utility" endpoints that could be built from free, public-domain or clearly redistributable sources and that match what agents actually buy. Do not build them without Joshua's approval.

Each gets: pydantic model, sample, provenance, tests, a stale gate, and a per-call price proposal. Keep the storage budget (store compact aggregates, not raw history forever).

## Phase 5: second payment rail (preparation only)
- Research the current docs for x402 (Coinbase facilitator or alternatives) and for an L402/x402/MPP proxy such as Alby's 402 Proxy. Report: setup steps, fees, free-tier limits, custody model (what wallet receives funds and who holds the key), and whether a stable-coin receiving address is needed.
- Design how AurAgrid would accept x402 alongside L402 without changing the data routes: a separate gateway path, same ledger with a `rail` column, same stale gate.
- Do NOT implement signing keys, wallets or accounts. Write `X402_PLAN.md` with the exact steps for Joshua, the risks (custody, volatility, tax/regulatory questions he should check), and what he must decide.

**Decision point 2:** Joshua decides whether to proceed with x402. Do nothing further on this rail until he does.

## Phase 6: listings (authorized; follow hard rule 3)
Research where a machine-readable paid API can be listed and do the listings yourself, in this order, once the discovery files, sample routes and (for MCP) the server exist and pass verification:
- **L402 directories:** Satring (submission is at its `/submit` page and costs about 1,000 sats at the time of research; ownership can be proven with a `.well-known/satring-verify` challenge; fetch the current docs), and L402 Apps.
- **402 Index:** find out whether direct submission exists; if it only indexes other sources, say so and make sure the sources it reads from list AurAgrid.
- **Coinbase Agentic.Market / x402 Bazaar:** only after Joshua decides on x402 in Phase 5.
- **MCP registries:** the Official MCP Registry, Smithery, Glama, PulseMCP, once the MCP server is published (publishing a package is also authorized, free registries only).
- **Awesome-lists:** open one pull request each (for example awesome-x402, awesome-mcp-servers, awesome-L402 if it exists), following each repo's contribution rules, with one honest line per entry.
- **Anything else you find** that fits (API directories, agent-tool catalogs, framework tool lists such as LangChain integrations), if free and consistent with hard rule 3.
- Use one set of descriptions (one line, about 60 words, full) built only from verified facts: feeds, sources, update intervals, free samples, signed responses, prices, contact. No "real-time", "institutional-grade", customer or revenue claims.
- After each submission, check that the listing is live and resolves, record it in `LISTINGS.md`, and add the listing to the weekly self-check in Phase 7.

## Phase 7: self-check loop (no spending, no self-editing)
A systemd timer, weekly, that:
- Fetches the service's own `/openapi.json`, `/llms.txt`, manifest and `l402.json` from the public URL and validates them.
- Calls each sample and each 402 challenge (never pays), checks `as_of`, schema and status.
- Checks the TLS certificate expiry and warns under 3 days.
- Writes a one-line-per-check report to `/var/log/auragrid/weekly.txt` and shows failures on the dashboard.
It must be read-only with respect to the system, with no ability to pay or restart services.

## Phase 8: verify and report
- Run the tests, adapt `verify_deployment.py` to the current endpoints, and run it from outside.
- One real small purchase by Joshua from a separate wallet: confirm the ledger records it under the right feed.
- Final report to Joshua: what changed (CHANGELOG), the price table awaiting approval, `LISTINGS.md` (every listing live, pending or blocked, with what Joshua must do for each blocked one: logins, codes, fees), the x402 decision, domain/TLS if missing, rollback steps, and honest expectations: the research found that most agents calling paid endpoints have no funded wallet, so revenue may stay near zero for months. The review date is 90 days after the listings go live.
