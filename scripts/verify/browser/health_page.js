// Health page refreshes with every API call stubbed and held in the page (#415);
// no backend state. Each load is driven by hand through the captured 30s refresh.
const assert = require('node:assert/strict');
const { makeReporter } = require('./lib');

const HEALTH = '/api/health', METRICS = '/api/metrics/latency';
const UNREADABLE = 'Service health could not be read: The server sent a response the UI could not read.';
const service = (status, latency_ms, model) => model ? { status, latency_ms, model } : { status, latency_ms };
const healthBody = (embed = 'ok') => ({
  status: embed === 'ok' ? 'ok' : 'degraded',
  services: {
    weaviate: service('ok', 11),
    ollama: { llm: service('ok', 22, 'phi3.5'), embed: service(embed, 33, 'nomic-embed-text') },
  },
  resources: { memory: { status: 'ok', allocated_gb: 11.67, recommended_minimum_gb: 12.0 } },
});
const metricsBody = n => ({
  total_records: n,
  retrieval_latency: { p50: 10, p95: 20, p99: 30 },
  llm_latency: { p50: 100, p95: 200, p99: 300 },
  total_latency: { p50: 110, p95: 220, p99: 330 },
  history: Array.from({ length: n }, (_, i) => ({ timestamp: new Date(Date.UTC(2026, 9, 1, 0, 0, i)).toISOString(), collection: 'OwnedFixture', retrieval_mode: 'hybrid', retrieval_ms: 10, llm_ms: 100, total_ms: 110 })),
});
const OK = { status: 200, body: healthBody() };
const DEGRADED = { status: 503, body: healthBody('error') };
const ERROR = message => ({ status: 500, body: { error: { code: 'INTERNAL_ERROR', message } } });

async function fixture(browser, base) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.evaluateOnNewDocument((health, metrics) => {
    sessionStorage.setItem('rag_role', JSON.stringify({ role: 'engineer' }));
    const original = window.setInterval;
    window.setInterval = (fn, delay, ...args) => {
      if (delay === 30000) { window.__healthRefresh = fn; return 45003; }
      return original(fn, delay, ...args);
    };
    const realFetch = window.fetch.bind(window);
    window.healthFixture = { requests: [] };
    window.fetch = (input, init = {}) => {
      const path = new URL(input, location.href).pathname;
      if (!path.startsWith('/api/')) return realFetch(input, init);
      if (path === '/api/goldstandard/diagnostics') return Promise.resolve(new Response(JSON.stringify({ issues: [] }), { status: 200 }));
      if (path === health || path === metrics) {
        const entry = { path, done: false };
        window.healthFixture.requests.push(entry);
        return new Promise((resolve, reject) => {
          entry.release = ({ status = 200, body, text, fail }) => {
            entry.done = true;
            if (fail) return reject(new TypeError('Failed to fetch'));
            resolve(new Response(text !== undefined ? text : JSON.stringify(body), { status }));
          };
        });
      }
      return Promise.reject(new Error('Unexpected fixture request ' + path));
    };
  }, HEALTH, METRICS);
  await page.goto(base + '/health', { waitUntil: 'domcontentloaded' });
  await requested(page, 1);
  return { ctx, page, errors };
}
// Wait until `n` loads have asked for both health and metrics.
async function requested(page, n) {
  await page.waitForFunction((h, m, count) => ['h', 'm'].every((_, i) =>
    window.healthFixture.requests.filter(r => r.path === [h, m][i]).length >= count), { timeout: 5000 }, HEALTH, METRICS, n);
}
// Answer load `load` (1 = the first) for `path`, then let React render.
async function release(page, path, load, answer) {
  await page.evaluate(async (p, i, a) => {
    const entry = window.healthFixture.requests.filter(r => r.path === p)[i - 1];
    if (!entry || entry.done) throw new Error(`no pending ${p} for load ${i}`);
    entry.release(a);
    await new Promise(r => setTimeout(r, 50));
    for (let k = 0; k < 2; k++) await new Promise(r => requestAnimationFrame(() => r()));
  }, path, load, answer);
}
async function refresh(page, n) {
  await page.evaluate(() => window.__healthRefresh());
  await requested(page, n);
}
async function load(page, n, health, metrics) {
  await release(page, HEALTH, n, health);
  await release(page, METRICS, n, metrics);
}
const text = page => page.evaluate(() => (document.querySelector('#root')?.innerText || '').replace(/\s+/g, ' ').trim());
// Each service card's label and dot colour; the latency tiles have no dot.
const cards = page => page.evaluate(() => [...document.querySelectorAll('span.rounded-full.w-2')].map(dot => ({
  label: (dot.parentElement.textContent || '').trim(),
  colour: dot.classList.contains('bg-red-500') ? 'red' : dot.classList.contains('bg-green-500') ? 'green' : '?',
})).map(c => `${c.label.split(' ')[0]}=${c.colour}`).join(', '));
const ALL_GREEN = 'Weaviate=green, LLM=green, Embed=green';
const EMBED_RED = 'Weaviate=green, LLM=green, Embed=red';
const charts = n => `Latency Trends (${n} queries in ring buffer)`;
const healthAlert = t => (t.match(/Service health could not be read:.{0,80}/) || [''])[0];

async function assertCharts(page, n) {
  const t = await text(page);
  assert.ok(t.includes(charts(n)), `expected "${charts(n)}" in: ${t.slice(0, 300)}`);
}
async function assertUnreadable(page, message) {
  const t = await text(page);
  assert.ok(t.includes(message), `expected "${message}" in: ${t.slice(0, 300)}`);
  assert.equal(await cards(page), '');
}

async function runHealthPageTests(browser, base, reporter) {
  reporter.section('Health page refreshes (#415, stubbed API)');
  const cases = [
    ['a failed metrics call (API error) keeps the last charts', async page => {
      await load(page, 1, OK, { body: metricsBody(2) });
      await refresh(page, 2);
      await load(page, 2, DEGRADED, ERROR('Synthetic metrics failure'));
      await assertCharts(page, 2);
      assert.equal(await cards(page), EMBED_RED, 'the same refresh\'s health is shown');
    }],
    ['a failed metrics call (no answer) keeps the last charts', async page => {
      await load(page, 1, OK, { body: metricsBody(2) });
      await refresh(page, 2);
      await load(page, 2, DEGRADED, { fail: true });
      await assertCharts(page, 2);
      assert.equal(await cards(page), EMBED_RED, 'the same refresh\'s health is shown');
    }],
    ['a JSON 200 without the health shape is unreadable, not a page error', async page => {
      await load(page, 1, { status: 200, body: { status: 'ok' } }, { body: metricsBody(1) });
      await assertUnreadable(page, UNREADABLE);
      await assertCharts(page, 1);
    }],
    ['an empty 200 is unreadable', async page => {
      await load(page, 1, { status: 200, text: '' }, { body: metricsBody(1) });
      await assertUnreadable(page, UNREADABLE);
    }],
    ['a non-JSON 200 is unreadable', async page => {
      await load(page, 1, { status: 200, text: '<html><body>Not the API</body></html>' }, { body: metricsBody(1) });
      await assertUnreadable(page, UNREADABLE);
    }],
    ['a 503 with the JSON error envelope shows its message, not cards', async page => {
      await load(page, 1, OK, { body: metricsBody(1) });
      await refresh(page, 2);
      await load(page, 2, { status: 503, body: { error: { code: 'SERVICE_UNAVAILABLE', message: 'Synthetic health envelope' } } }, { body: metricsBody(1) });
      await assertUnreadable(page, 'Service health could not be read: Synthetic health envelope');
    }],
    ['a late older health failure cannot replace a newer success', async page => {
      await load(page, 1, DEGRADED, { body: metricsBody(1) });
      await refresh(page, 2); await refresh(page, 3);
      await release(page, HEALTH, 3, OK);
      await release(page, HEALTH, 2, { fail: true });
      const t = await text(page);
      assert.equal(healthAlert(t), '');
      assert.equal(await cards(page), ALL_GREEN);
    }],
    ['a late older health success cannot replace a newer failure', async page => {
      await load(page, 1, OK, { body: metricsBody(1) });
      await refresh(page, 2); await refresh(page, 3);
      await release(page, HEALTH, 3, { status: 502, text: '<html>502 Bad Gateway</html>' });
      await release(page, HEALTH, 2, DEGRADED);
      await assertUnreadable(page, 'Service health could not be read: The API is not responding');
    }],
    ['a late older metrics success cannot replace newer charts', async page => {
      await load(page, 1, OK, { body: metricsBody(1) });
      await refresh(page, 2); await refresh(page, 3);
      await release(page, METRICS, 3, { body: metricsBody(5) });
      await release(page, METRICS, 2, { body: metricsBody(3) });
      await assertCharts(page, 5);
    }],
    ['a late older metrics success after a newer failure is shown', async page => {
      await refresh(page, 2);
      await release(page, METRICS, 2, ERROR('Synthetic metrics failure'));
      await release(page, METRICS, 1, { body: metricsBody(4) });
      await assertCharts(page, 4);
    }],
  ];
  for (const [name, test] of cases) {
    let s;
    try { s = await fixture(browser, base); await test(s.page); assert.deepEqual(s.errors, []); reporter.check(name, true); }
    catch (e) { reporter.check(name, false, e.stack || e.message); }
    finally { if (s) await s.ctx.close(); }
  }
}
module.exports = { runHealthPageTests };
if (require.main === module) (async () => {
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runHealthPageTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
