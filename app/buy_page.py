"""Browser checkout for /buy: pay an L402 invoice with WebLN (or any wallet).

Flow: request the paid path -> Aperture answers 402 with a macaroon and an
invoice -> the wallet pays and returns the preimage -> replay the request with
'Authorization: L402 <macaroon>:<preimage>'. Tokens are kept in localStorage
so a 30-day pass keeps working on return visits. The page loads nothing from
third parties and is served with a nonce-based CSP.
"""

BUY_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AurAgrid - Buy data access</title>
<style nonce="__NONCE__">
  :root { color-scheme: dark; }
  body { margin: 0; background: #0f172a; color: #e2e8f0; font: 15px/1.5 system-ui, sans-serif; }
  main { max-width: 860px; margin: 0 auto; padding: 32px 20px 64px; }
  h1 { font-size: 26px; margin: 0 0 4px; color: #34d399; letter-spacing: .04em; }
  h2 { font-size: 16px; margin: 32px 0 12px; color: #94a3b8; text-transform: uppercase; letter-spacing: .08em; }
  p.lead { color: #94a3b8; margin: 0 0 8px; }
  .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 16px 18px; margin: 10px 0;
          display: flex; justify-content: space-between; align-items: center; gap: 16px; }
  .card .name { font-weight: 600; color: #f8fafc; }
  .card .meta { font-size: 13px; color: #94a3b8; }
  .price { font-weight: 700; color: #34d399; white-space: nowrap; text-align: right; }
  button { background: #10b981; color: #052e16; border: 0; border-radius: 8px; padding: 8px 14px;
           font-weight: 700; cursor: pointer; }
  button.secondary { background: #334155; color: #e2e8f0; }
  button:disabled { opacity: .5; cursor: default; }
  #panel { display: none; background: #111827; border: 1px solid #10b981; border-radius: 12px; padding: 18px; margin-top: 24px; }
  code, pre, textarea { font-family: ui-monospace, monospace; font-size: 12px; }
  textarea { width: 100%; box-sizing: border-box; background: #0b1220; color: #e2e8f0; border: 1px solid #334155;
             border-radius: 8px; padding: 8px; }
  pre { background: #0b1220; border: 1px solid #334155; border-radius: 8px; padding: 12px; overflow: auto; max-height: 360px; }
  .row { display: flex; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
  .status { margin: 8px 0; color: #fbbf24; }
  .error { color: #f87171; }
  a { color: #38bdf8; }
</style>
</head>
<body>
<main>
  <h1>AurAgrid</h1>
  <p class="lead">Pay with Lightning. No account: your payment proof is your access token.</p>
  <p class="lead">Wallet with WebLN (Alby and similar) pays in one click. Any other Lightning wallet works too:
     pay the invoice, then paste the preimage.</p>

  <h2>30-day passes and bundles</h2>
  <div id="plans"><div class="meta">Loading...</div></div>

  <h2>Paid endpoints (hourly access)</h2>
  <div id="hourly"><div class="meta">Loading...</div></div>

  <h2>Free samples</h2>
  <div id="samples"></div>

  <section id="panel" aria-live="polite">
    <div class="name" id="p-title"></div>
    <div class="status" id="p-status"></div>
    <div id="p-invoice-box" hidden>
      <div class="meta">Lightning invoice</div>
      <textarea id="p-invoice" rows="4" readonly></textarea>
      <div class="row">
        <button class="secondary" id="p-copy" type="button">Copy invoice</button>
        <a id="p-open" href="#">Open in wallet</a>
      </div>
      <div class="meta">Paid from another wallet? Paste the payment preimage (64 hex characters):</div>
      <div class="row">
        <input id="p-preimage" size="66" autocomplete="off" spellcheck="false">
        <button id="p-submit" type="button">Unlock</button>
      </div>
    </div>
    <div id="p-token-box" hidden>
      <div class="meta">Your access token (keep it private; reuse it until it expires):</div>
      <textarea id="p-token" rows="3" readonly></textarea>
      <div class="meta">curl example:</div>
      <pre id="p-curl"></pre>
      <div class="meta">Response:</div>
      <pre id="p-result"></pre>
    </div>
  </section>
</main>
<script nonce="__NONCE__">
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const HEX64 = /^[0-9a-f]{64}$/i;
  const STORE = "auragrid_tokens";
  let current = null;   // {path, macaroon, invoice, title, ttl}

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function loadTokens() {
    try { return JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { return {}; }
  }
  function saveToken(path, token, ttl) {
    const all = loadTokens();
    all[path] = { token: token, expires: Date.now() + (ttl || 3600) * 1000 };
    try { localStorage.setItem(STORE, JSON.stringify(all)); } catch (e) {}
  }
  function validToken(path) {
    const t = loadTokens()[path];
    return t && t.expires > Date.now() ? t.token : null;
  }

  function priceText(item) {
    let s = item.price_sats != null ? Number(item.price_sats).toLocaleString() + " sats" : "price n/a";
    if (item.usd_price != null) s += " (~$" + Number(item.usd_price).toFixed(2) + ")";
    return s;
  }

  function card(title, meta, item, actions) {
    const c = el("div", "card");
    const left = el("div");
    left.append(el("div", "name", title), el("div", "meta", meta));
    const right = el("div", "price", item ? priceText(item) : "");
    const row = el("div", "row");
    actions.forEach((a) => row.appendChild(a));
    right.appendChild(row);
    c.append(left, right);
    return c;
  }

  function button(label, path, title, ttl, secondary) {
    const b = el("button", secondary ? "secondary" : "", label);
    b.type = "button";
    b.addEventListener("click", () => buy(path, title, ttl));
    return b;
  }

  function render(manifest) {
    const plans = $("plans"), hourly = $("hourly"), samples = $("samples");
    plans.replaceChildren(); hourly.replaceChildren(); samples.replaceChildren();
    (manifest.plans || []).forEach((p) => {
      const days = Math.round((p.token_ttl_seconds || 0) / 86400);
      const actions = p.paths.map((path) => {
        const feed = path.split("/").pop();
        const label = (validToken(path) ? "Open " : (p.paths.length > 1 ? "Buy / open " : "Buy ")) + feed;
        return button(label, path, p.title, p.token_ttl_seconds, p.paths.length > 1 && path !== p.paths[0]);
      });
      plans.appendChild(card(p.title, days + "-day token, one payment" +
        (p.paths.length > 1 ? "; one token opens all " + p.paths.length + " feeds" : ""), p, actions));
    });
    const paid = (manifest.endpoints || []).filter((e) => e.access === "paid" && e.status === "live");
    if (!(manifest.plans || []).length) plans.appendChild(el("div", "meta", "No passes on sale right now."));
    if (!paid.length) {
      const note = el("div", "meta", "Nothing to buy here right now: every live endpoint is free. ");
      const a = el("a", "", "See the free endpoints");
      a.href = "/llms.txt";
      note.appendChild(a);
      hourly.appendChild(note);
    }
    paid.forEach((e) => {
      const hours = Math.round((e.token_ttl_seconds || 3600) / 3600);
      hourly.appendChild(card(e.name, e.description + " Token valid " + hours + "h.", e,
        [button(validToken(e.path) ? "Open" : "Buy", e.path, e.name, e.token_ttl_seconds)]));
    });
    (manifest.samples || []).forEach((s) => {
      const a = el("a", "", s.path);
      a.href = s.path;
      const meta = el("div", "meta");
      meta.append(a, document.createTextNode(" - delayed " + Math.round(s.delay_seconds / 60) +
        " min, lists cut to " + s.max_items_per_list + " items"));
      samples.appendChild(meta);
    });
  }

  function status(text, isError) {
    const s = $("p-status");
    s.textContent = text;
    s.className = "status" + (isError ? " error" : "");
  }

  function parseChallenge(header) {
    const m = /L402\\s+macaroon="([^"]+)",\\s*invoice="([^"]+)"/i.exec(header || "");
    return m ? { macaroon: m[1], invoice: m[2] } : null;
  }

  async function unlock(token) {
    const r = await fetch(current.path, { headers: { Authorization: token }, cache: "no-store" });
    const text = await r.text();
    if (!r.ok) throw new Error("server answered HTTP " + r.status + ": " + text.slice(0, 200));
    saveToken(current.path, token, current.ttl);
    $("p-invoice-box").hidden = true;
    $("p-token-box").hidden = false;
    $("p-token").value = token;
    $("p-curl").textContent = "curl -H 'Authorization: " + token + "' " + location.origin + current.path;
    try { $("p-result").textContent = JSON.stringify(JSON.parse(text), null, 2); }
    catch (e) { $("p-result").textContent = text; }
    status("Unlocked. This token keeps working until it expires.");
  }

  async function buy(path, title, ttl) {
    current = { path: path, title: title, ttl: ttl };
    $("panel").style.display = "block";
    $("p-title").textContent = title + " - " + path;
    $("p-invoice-box").hidden = true;
    $("p-token-box").hidden = true;
    $("panel").scrollIntoView({ behavior: "smooth" });

    const saved = validToken(path);
    if (saved) {
      status("Using your saved token...");
      try { await unlock(saved); return; } catch (e) { status("Saved token no longer works; buying a new one."); }
    }

    status("Requesting an invoice...");
    let challenge;
    try {
      const r = await fetch(path, { cache: "no-store" });
      if (r.status !== 402) throw new Error("expected 402 Payment Required, got HTTP " + r.status);
      challenge = parseChallenge(r.headers.get("WWW-Authenticate"));
      if (!challenge) throw new Error("no L402 challenge in the response");
    } catch (e) { status("Could not get an invoice: " + e.message, true); return; }

    current.macaroon = challenge.macaroon;
    current.invoice = challenge.invoice;
    $("p-invoice").value = challenge.invoice;
    $("p-open").href = "lightning:" + challenge.invoice;
    $("p-preimage").value = "";
    $("p-invoice-box").hidden = false;

    if (!window.webln) { status("No WebLN wallet detected: pay the invoice with any wallet, then paste the preimage."); return; }
    try {
      status("Confirm the payment in your wallet...");
      await window.webln.enable();
      const res = await window.webln.sendPayment(challenge.invoice);
      if (!res || !HEX64.test(res.preimage || "")) throw new Error("wallet did not return a preimage");
      status("Paid. Unlocking...");
      await unlock("L402 " + challenge.macaroon + ":" + res.preimage.toLowerCase());
    } catch (e) {
      status("Wallet payment did not complete (" + (e.message || e) + "). You can still pay the invoice and paste the preimage.", true);
    }
  }

  $("p-submit").addEventListener("click", async () => {
    const pre = $("p-preimage").value.trim();
    if (!current || !current.macaroon) return;
    if (!HEX64.test(pre)) { status("A preimage is 64 hex characters.", true); return; }
    try { status("Unlocking..."); await unlock("L402 " + current.macaroon + ":" + pre.toLowerCase()); }
    catch (e) { status("Unlock failed: " + e.message, true); }
  });
  $("p-copy").addEventListener("click", () => {
    navigator.clipboard && navigator.clipboard.writeText($("p-invoice").value);
  });

  fetch("/agent-manifest.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(render)
    .catch((e) => { $("plans").replaceChildren(el("div", "meta error", "Could not load the catalog: " + e.message)); });
})();
</script>
</body>
</html>
"""
