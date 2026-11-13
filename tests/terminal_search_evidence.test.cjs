const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const page = fs.readFileSync(path.join(__dirname, '..', 'terminal-home.html'), 'utf8');
const ui = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'hub-ui-i18n.json'))).ui;
function section(start, end) {
  const from = page.indexOf(start), to = page.indexOf(end, from);
  assert.ok(from >= 0 && to > from);
  return page.slice(from, to);
}
function fixture() {
  const listeners = {};
  const directory = {hidden: true};
  const cards = {innerHTML: ''};
  const requests = [];
  const ctx = {
    URL, Date, lang: 'en', HUB: 'https://hub.example',
    input: {value: 'weather'},
    drop: {innerHTML: '', classList: {add() {}}},
    form: {addEventListener(name, callback) { listeners[name] = callback; }},
    t(key, vars, fallback) {
      let value = ui[ctx.lang][key] || fallback || key;
      for (const [name, term] of Object.entries(vars || {})) value = value.replace(`{${name}}`, String(term));
      return value;
    },
    hostOf: u => u === 'local' ? '' : new URL(u).host,
    descForSearch: c => c.description || '',
    capabilityPrice: c => String(c.price_per_call_usd),
    accessMode: c => c.access_mode || 'public',
    usd: n => '$' + n,
    fetch: async () => ({json: async () => ctx.response}),
    fetchJson: async url => { requests.push(url); return ctx.response; },
    document: {getElementById(id) { return ctx.apex && (id === 'external-directory' ? directory : cards); }},
  };
  vm.createContext(ctx);
  vm.runInContext(section('  function esc(', '\n  function visitorId('), ctx);
  vm.runInContext(section('  let searchMatches = []', '\n  function showInvoke('), ctx);
  vm.runInContext(section("  form.addEventListener('submit',", "\n  document.querySelectorAll('.intent-example')"), ctx);
  return {
    ctx,
    render(matches, observed) { ctx.renderSearchResults(matches, {}, observed); return ctx.drop.innerHTML; },
    async search(response) { ctx.response = response; await listeners.submit({preventDefault() {}}); return ctx.drop.innerHTML; },
    async directory(external, apex = true) {
      ctx.apex = apex;
      ctx.response = {external};
      await ctx.loadExternalDirectory();
      return {hidden: directory.hidden, html: cards.innerHTML, requests};
    },
    matches() { return vm.runInContext('searchMatches', ctx); },
    relanguage(lang) {
      ctx.lang = lang;
      vm.runInContext('renderSearchResults(searchMatches, searchMetaState, searchObserved, searchExternal)', ctx);
      return ctx.drop.innerHTML;
    },
  };
}
const quote = {
  seller: 'https://seller.example', seller_name: 'Weather seller',
  url: 'https://seller.example/weather', method: 'POST', description: 'Weather',
  price_usd: 0.003, pay_to: '0x123', network: 'eip155:8453',
  observed_by: 'https://hub.example', observed_at: '2026-10-31T10:00:00Z',
};
const catalog = {name: 'Weather catalog', source_hub: 'https://catalog.example', description: 'weather'};
const externalDefaults = {payment_protocol: 'x402', payment_networks: ['base'], asset: 'USDC',
  published_price_usd: 0.01, price_basis: 'per_request', payment_recipient: 'provider', hub_invoke: false};
const cmc = {...externalDefaults, id: 'coinmarketcap', name: 'CoinMarketCap', description: 'Market quotes',
  documentation_url: 'https://coinmarketcap.com/api/documentation/ai-agent-hub/x402',
  endpoint: 'https://pro-api.coinmarketcap.com/x402/v3/cryptocurrency/quotes/latest'};
const tavily = {...externalDefaults, id: 'tavily', name: 'Tavily', description: 'Web search',
  documentation_url: 'https://docs.tavily.com/documentation/machine-payments/x402', endpoint: 'https://x402.tavily.com/search'};
const laso = {...externalDefaults, id: 'laso', name: 'Laso', description: 'Prepaid cards',
  documentation_url: 'https://docs.laso.finance/guides/usa-prepaid-card', endpoint: 'https://laso.finance/get-card',
  payment_networks: ['base', 'solana'], price_basis: 'minimum_load', published_price_usd: 5};

test('observed-only search is a result, never a sandbox invocation', async () => {
  const f = fixture();
  const html = await f.search({matches: [], observed: [quote]});
  assert.match(html, /Observed offers/);
  assert.doesNotMatch(html, /data-idx|click to try free/);
  assert.ok(!html.includes(ui.en.no_matches));
  assert.ok(!html.includes(ui.en.search_exact_fallback));
  assert.equal(f.matches().length, 0);
  assert.match(html, /href="https:\/\/seller.example\/weather"/);
  assert.match(html, /noopener noreferrer/);
  assert.match(html, /Seller did not sign this listing/);
  assert.match(html, /2026-10-31 10:00:00 UTC/);
});

test('catalog indexes remain correct with filtered and observed results', () => {
  const f = fixture();
  const html = f.render([{...catalog, access_mode: 'operator_gated'}, catalog], [quote]);
  assert.equal(f.matches().length, 1);
  assert.match(html, /data-idx="0"/);
  assert.doesNotMatch(html, /data-idx="1"/);
  assert.ok(html.indexOf('Catalogue offers') < html.indexOf('Observed offers'));
});

test('only recorded catalogue verification produces a signed badge', () => {
  const f = fixture();
  let html = f.render([{...catalog, provider_pubkey: 'key', trust_score: 1}], []);
  assert.match(html, /verification not recorded/);
  assert.ok(!html.includes(ui.en.evidence_signed));
  html = f.render([{...catalog, evidence: {
    kind: 'signed_catalogue', scope: 'catalogue', signer: 'https://catalog.example',
    signer_public_key: 'a-public-key-from-the-verification', checked_at: '2026-10-30T09:00:00Z',
    checked_by: 'https://hub.example',
  }}], []);
  assert.ok(html.includes(ui.en.evidence_signed));
  assert.match(html, /https:\/\/catalog.example/);
  assert.match(html, /2026-10-30 09:00:00 UTC/);
  assert.match(html, /title="a-public-key-from-the-verification"/);
});

test('observed offers cannot promote their own metadata into a signed badge', () => {
  const f = fixture();
  const html = f.render([], [{...quote, evidence: {kind: 'signed_catalogue', scope: 'catalogue', signer_public_key: 'key', signer: 'x', checked_at: quote.observed_at}}]);
  assert.ok(!html.includes(ui.en.evidence_signed));
});

test('seller text is escaped and unsafe endpoint links are omitted', () => {
  const f = fixture();
  for (const url of ['javascript:alert(1)', 'https://other.example/pay', 'https://name:password@seller.example/pay']) {
    const html = f.render([], [{...quote, url, seller_name: '<img src=x onerror=alert(1)>', observed_by: '<script>alert(1)</script>'}]);
    assert.doesNotMatch(html, /<script>|<img|class="search-seller-link"/);
    assert.match(html, /&lt;img/);
  }
});

test('language changes preserve observed-only results in all five languages', () => {
  const f = fixture(); f.render([], [quote]);
  for (const lang of Object.keys(ui)) {
    const html = f.relanguage(lang);
    assert.ok(html.includes(ui[lang].search_observed));
    assert.ok(html.includes(ui[lang].evidence_unsigned));
    assert.ok(html.includes('https://seller.example/weather'));
  }
});

test('empty search resets prior results and missing timestamps stay unknown', async () => {
  const f = fixture();
  const html = f.render([{...catalog, source_hub: 'local'}], [{...quote, observed_at: '<script>'}]);
  assert.ok(html.includes(ui.en.evidence_local));
  assert.ok(html.includes(ui.en.evidence_unknown));
  assert.doesNotMatch(html, /datetime="undefined|<script>/);
  const empty = await f.search({matches: [], observed: []});
  assert.ok(empty.includes(ui.en.no_matches));
  assert.equal(f.matches().length, 0);
});

test('external-only search links to first-party documentation without sandbox invocation', async () => {
  const f = fixture();
  const html = await f.search({matches: [], observed: [], external: [laso]});
  assert.match(html, /Direct x402 services/);
  assert.match(html, /5 USDC minimum load · x402 · Base · Solana/);
  assert.match(html, /href="https:\/\/docs.laso.finance\/guides\/usa-prepaid-card"/);
  assert.doesNotMatch(html, /get-card|data-idx|search-readout|Observed offers/);
  assert.ok(!html.includes(ui.en.no_matches));
  assert.equal(f.matches().length, 0);
  for (const lang of Object.keys(ui)) assert.ok(f.relanguage(lang).includes(ui[lang].external_title));
  assert.ok((await f.search({matches: [], observed: [], external: []})).includes(ui.zh.no_matches));
});

test('home directory renders all three cards and stays absent on other hubs', async () => {
  const f = fixture();
  const hidden = await f.directory([cmc, tavily, laso], false);
  assert.equal(hidden.hidden, true);
  assert.deepEqual(hidden.requests, []);
  const result = await f.directory([cmc, tavily, laso]);
  assert.equal(result.hidden, false);
  assert.deepEqual(result.requests, ['https://hub.example/ai-market/v2/external-services']);
  assert.equal((result.html.match(/class="external-service"/g) || []).length, 3);
  for (const provider of [cmc, tavily, laso]) assert.ok(result.html.includes(provider.documentation_url));
  assert.match(result.html, /href="https:\/\/coinmarketcap.com\/api\/documentation\/ai-agent-hub\/x402"\s+target="_blank" rel="noopener noreferrer" title="Provider documentation">CoinMarketCap<\/a>/);
  assert.doesNotMatch(result.html, /pro-api.coinmarketcap|x402.tavily.com|laso.finance\/get-card/);
  const empty = await f.directory([]);
  assert.equal(empty.hidden, true);
});

test('external entries never change callable catalogue indexes or accept unsafe documentation links', async () => {
  const f = fixture();
  const html = await f.search({matches: [catalog], observed: [quote], external: [
    {...tavily, name: '<img src=x>', documentation_url: 'javascript:alert(1)'},
    {...tavily, documentation_url: tavily.endpoint}, {...laso, hub_invoke: true},
    {...cmc, name: '<script>CoinMarketCap</script>'},
  ]});
  assert.equal(f.matches().length, 1);
  assert.equal((html.match(/data-idx=/g) || []).length, 1);
  assert.equal((html.match(/class="external-service"/g) || []).length, 1);
  assert.match(html, /&lt;script&gt;CoinMarketCap&lt;\/script&gt;/);
  assert.doesNotMatch(html, /javascript:|<script>|<img|x402.tavily.com/);
});
