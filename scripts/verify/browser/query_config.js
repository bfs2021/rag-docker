// Deferred responses exercise the real provider, Retrieval page, and Q&A page.
// All API calls are intercepted before application startup; no backend is used.
const assert = require('node:assert/strict');
const { sleep, makeReporter, setValue, clickByText } = require('./lib');

const A = { collection: 'FixtureA', retrieval_mode: 'hybrid', top_k: 11, alpha: 0.25, ef: null, response_format: 'engineer', is_default: false };
const B = { ...A, collection: 'FixtureB', retrieval_mode: 'semantic', top_k: 23, alpha: 0.6 };
const DEFAULT = { ...B, retrieval_mode: 'hnsw', top_k: 5, alpha: 0.75, is_default: true };

async function fixture(browser, base) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.evaluateOnNewDocument((a, b) => {
    sessionStorage.setItem('rag_role', JSON.stringify({ role: 'engineer' }));
    const realFetch = window.fetch.bind(window);
    const requests = [];
    window.queryConfigFixture = { requests };
    window.fetch = (input, init = {}) => {
      const path = new URL(input, location.href).pathname;
      if (!path.startsWith('/api/')) return realFetch(input, init);
      const method = init.method || 'GET';
      const body = init.body ? JSON.parse(init.body) : null;
      const entry = { path, method, body, done: false };
      requests.push(entry);
      const response = data => new Response(JSON.stringify(data), { status: 200 });
      if (path === '/api/collections') {
        entry.done = true;
        return Promise.resolve(response({ collections: [a, b].map(c => ({ name: c.collection, object_count: 1, index_type: 'hnsw', distance_metric: 'cosine' })) }));
      }
      if (path === '/api/retrieval/config/FixtureA' && requests.filter(r => r.path === path).length === 1) {
        entry.done = true;
        return Promise.resolve(response(a));
      }
      if (path === '/api/query') {
        entry.done = true;
        return Promise.resolve(response(window.queryConfigFixture.queryResult || { answer: 'Fixture answer', citations: [], retrieval_latency_ms: 1, llm_latency_ms: 1 }));
      }
      if (path.startsWith('/api/retrieval/config')) {
        return new Promise(resolve => { entry.resolve = (data, status) => { entry.done = true; resolve(new Response(JSON.stringify(data), { status })); }; });
      }
      return Promise.reject(new Error('Unexpected fixture API call: ' + method + ' ' + path));
    };
  }, A, B);
  await page.goto(base + '/retrieval', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.body.innerText.includes('Top-K Results: 11') && !document.body.innerText.includes('Loading saved settings'));
  return { ctx, page, errors };
}

async function pending(page, path, method = 'GET', count = 1) {
  await page.waitForFunction((p, m, n) => window.queryConfigFixture.requests.filter(r => r.path === p && r.method === m && !r.done).length >= n, {}, path, method, count);
}

async function release(page, path, data, { method = 'GET', status = 200, last = false } = {}) {
  await pending(page, path, method);
  await page.evaluate(async (p, m, d, s, latest) => {
    const waiting = window.queryConfigFixture.requests.filter(r => r.path === p && r.method === m && !r.done);
    (latest ? waiting[waiting.length - 1] : waiting[0]).resolve(d, s);
    // Drain fetch/text microtasks and React's committed effects without sleeps.
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  }, path, method, data, status, last);
}

async function save(page) {
  assert.equal(await clickByText(page, 'Save for this collection'), true);
  await pending(page, '/api/retrieval/config', 'POST');
}

async function select(page, name) {
  await page.select('select', name);
  await pending(page, '/api/retrieval/config/' + name);
}

async function assertSettings(page, config) {
  assert.deepEqual(await page.evaluate(() => ({
    collection: document.querySelector('select').value,
    top_k: Number(document.querySelector('input[type=range]').value),
    retrieval_mode: document.querySelector('input[name=mode]:checked').value,
    loading: document.body.innerText.includes('Loading saved settings'),
  })), { collection: config.collection, top_k: config.top_k, retrieval_mode: config.retrieval_mode, loading: false });
}

async function assertQuery(page, config) {
  assert.equal(await clickByText(page, 'Q&A'), true);
  await page.waitForSelector('textarea');
  await setValue(page, '() => document.querySelector("textarea")', 'Which settings are active?');
  assert.equal(await clickByText(page, 'Ask'), true);
  await page.waitForFunction(() => window.queryConfigFixture.requests.some(r => r.path === '/api/query'));
  const body = await page.evaluate(() => window.queryConfigFixture.requests.find(r => r.path === '/api/query').body);
  assert.deepEqual(body, {
    question: 'Which settings are active?', collection: config.collection,
    retrieval_mode: config.retrieval_mode, top_k: config.top_k, alpha: config.alpha,
    include_citations: false, response_format: 'engineer',
  });
}

async function runQueryConfigTests(browser, base, reporter) {
  reporter.section('collection-bound query configuration (deferred API fixtures)');
  const cases = [];
  for (const saveFirst of [true, false]) {
    cases.push([`A save ${saveFirst ? 'before' : 'after'} B load preserves B settings and next query`, async page => {
      await save(page);
      await select(page, B.collection);
      if (saveFirst) {
        await release(page, '/api/retrieval/config', A, { method: 'POST' });
        assert.equal(await page.evaluate(() => document.body.innerText.includes('Loading saved settings')), true);
      }
      await release(page, '/api/retrieval/config/' + B.collection, B);
      if (!saveFirst) await release(page, '/api/retrieval/config', A, { method: 'POST' });
      await assertSettings(page, B);
      await assertQuery(page, B);
    }]);
  }
  cases.push(['B load failure remains authoritative after stale A save', async page => {
    await save(page);
    await select(page, B.collection);
    await release(page, '/api/retrieval/config/' + B.collection, { error: { message: 'Fixture B unavailable' } }, { status: 500 });
    await release(page, '/api/retrieval/config', A, { method: 'POST' });
    assert.equal(await page.evaluate(() => document.body.innerText.includes('Fixture B unavailable') && document.body.innerText.includes('showing defaults')), true);
    await assertSettings(page, DEFAULT);
    await assertQuery(page, DEFAULT);
  }]);
  cases.push(['A -> B -> A does not revive the first A save or stale B load', async page => {
    await save(page);
    await select(page, B.collection);
    await select(page, A.collection);
    const current = { ...A, retrieval_mode: 'semantic', top_k: 31, alpha: 0.9 };
    await release(page, '/api/retrieval/config/' + A.collection, current);
    await release(page, '/api/retrieval/config', A, { method: 'POST' });
    await release(page, '/api/retrieval/config/' + B.collection, B);
    await assertSettings(page, current);
    await assertQuery(page, current);
  }]);
  for (const latestFirst of [true, false]) {
    cases.push([`concurrent saves keep latest submitted values (${latestFirst ? 'latest' : 'oldest'} response first)`, async page => {
      await save(page);
      await setValue(page, '() => document.querySelector("input[type=range]")', '17');
      await save(page);
      await pending(page, '/api/retrieval/config', 'POST', 2);
      const current = { ...A, top_k: 17 };
      const bodies = await page.evaluate(() => window.queryConfigFixture.requests.filter(r => r.method === 'POST').map(r => r.body.top_k));
      assert.deepEqual(bodies, [11, 17]);
      await release(page, '/api/retrieval/config', latestFirst ? current : A, { method: 'POST', last: latestFirst });
      await release(page, '/api/retrieval/config', latestFirst ? A : current, { method: 'POST' });
      await assertSettings(page, current);
      await assertQuery(page, current);
    }]);
  }
  cases.push(['current save failure still reaches the caller and preserves saved settings', async page => {
    await save(page);
    await release(page, '/api/retrieval/config', { error: { message: 'Fixture save failed' } }, { method: 'POST', status: 500 });
    assert.equal(await page.evaluate(() => document.body.innerText.includes('Fixture save failed')), true);
    await assertSettings(page, A);
    await assertQuery(page, A);
  }]);
  // Reviewer cases: what a stale save leaves on the newly selected collection's page.
  cases.push(['stale A save still persists to A, and B shows no save confirmation', async page => {
    await save(page);
    const posted = await page.evaluate(() => window.queryConfigFixture.requests.find(r => r.method === 'POST').body.collection);
    assert.equal(posted, A.collection, 'the save request must name the collection it was started for');
    await select(page, B.collection);
    await release(page, '/api/retrieval/config/' + B.collection, B);
    await release(page, '/api/retrieval/config', A, { method: 'POST' });
    assert.equal(await page.evaluate(() => document.body.innerText.includes('Saved!')), false, 'B shows "Saved!" from A\'s stale save');
    await assertSettings(page, B);
  }]);
  cases.push(['stale A save failure is not shown as an error on B', async page => {
    await save(page);
    await select(page, B.collection);
    await release(page, '/api/retrieval/config/' + B.collection, B);
    await release(page, '/api/retrieval/config', { error: { message: 'Fixture stale save failed' } }, { method: 'POST', status: 500 });
    assert.equal(await page.evaluate(() => document.body.innerText.includes('Fixture stale save failed')), false, "B's page shows A's stale save failure");
    await assertSettings(page, B);
    await assertQuery(page, B);
  }]);
  for (const failureFirst of [true, false]) {
    cases.push([`older acknowledged save survives newer failure (${failureFirst ? 'failure' : 'success'} first)`, async page => {
      const saved = { ...A, top_k: 13 };
      await setValue(page, '() => document.querySelector("input[type=range]")', '13');
      await save(page);
      await setValue(page, '() => document.querySelector("input[type=range]")', '17');
      await save(page);
      await pending(page, '/api/retrieval/config', 'POST', 2);
      const fail = () => release(page, '/api/retrieval/config', { error: { message: 'Latest save failed' } }, { method: 'POST', status: 500, last: true });
      if (failureFirst) await fail();
      await release(page, '/api/retrieval/config', saved, { method: 'POST' });
      if (!failureFirst) await fail();
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Latest save failed')), true);
      assert.equal(await page.evaluate(() => document.body.innerText.includes('Saved!')), false);
      await assertSettings(page, saved);
      await assertQuery(page, saved);
    }]);
  }
  // Testing reviewer cases (#205): superseded saves in one selection, and the confirmation timer.
  const shows = (page, text) => page.evaluate(t => document.body.innerText.includes(t), text);
  cases.push(['superseded save shows no confirmation; only the latest save shows Saved!', async page => {
    await save(page);
    await setValue(page, '() => document.querySelector("input[type=range]")', '17');
    await save(page);
    await pending(page, '/api/retrieval/config', 'POST', 2);
    const current = { ...A, top_k: 17 };
    await release(page, '/api/retrieval/config', A, { method: 'POST' });
    assert.equal(await shows(page, 'Saved!'), false, 'the superseded save showed "Saved!"');
    await release(page, '/api/retrieval/config', current, { method: 'POST' });
    assert.equal(await shows(page, 'Saved!'), true, 'the latest save showed no "Saved!"');
    await assertSettings(page, current);
    await assertQuery(page, current);
  }]);
  cases.push(['superseded save failure is not shown; the latest success confirms', async page => {
    await save(page);
    await setValue(page, '() => document.querySelector("input[type=range]")', '17');
    await save(page);
    await pending(page, '/api/retrieval/config', 'POST', 2);
    const current = { ...A, top_k: 17 };
    await release(page, '/api/retrieval/config', { error: { message: 'Superseded save failed' } }, { method: 'POST', status: 500 });
    assert.equal(await shows(page, 'Superseded save failed'), false, "the superseded save's failure was shown");
    await release(page, '/api/retrieval/config', current, { method: 'POST' });
    assert.equal(await shows(page, 'Superseded save failed'), false, "the superseded save's failure was shown");
    assert.equal(await shows(page, 'Saved!'), true, 'the latest save showed no "Saved!"');
    await assertSettings(page, current);
    await assertQuery(page, current);
  }]);
  cases.push(["an earlier save's confirmation timer does not clear a later save's Saved!", async page => {
    await save(page);
    await release(page, '/api/retrieval/config', A, { method: 'POST' });
    assert.equal(await shows(page, 'Saved!'), true, 'the first save showed no "Saved!"');
    const first = Date.now();
    await sleep(2000);
    const current = { ...A, top_k: 17 };
    await setValue(page, '() => document.querySelector("input[type=range]")', '17');
    await save(page);
    await release(page, '/api/retrieval/config', current, { method: 'POST' });
    assert.equal(await shows(page, 'Saved!'), true, 'the second save showed no "Saved!"');
    await sleep(Math.max(0, first + 3600 - Date.now()));
    assert.equal(await shows(page, 'Saved!'), true, "the first save's timer cleared the second save's \"Saved!\"");
    await page.waitForFunction(() => !document.body.innerText.includes('Saved!'), { timeout: 5000 });
  }]);
  cases.push(['a late older success cannot republish an acknowledged save over new edits', async page => {
    await save(page);
    await setValue(page, '() => document.querySelector("input[type=range]")', '13');
    await save(page);
    await setValue(page, '() => document.querySelector("input[type=range]")', '17');
    await save(page);
    await pending(page, '/api/retrieval/config', 'POST', 3);
    await release(page, '/api/retrieval/config', { error: { message: 'Newest failed' } }, { method: 'POST', status: 500, last: true });
    await release(page, '/api/retrieval/config', { ...A, top_k: 13 }, { method: 'POST', last: true });
    await assertSettings(page, { ...A, top_k: 13 });
    await setValue(page, '() => document.querySelector("input[type=range]")', '19');
    await release(page, '/api/retrieval/config', A, { method: 'POST' });
    await assertSettings(page, { ...A, top_k: 19 });
    assert.equal(await page.evaluate(() => document.body.innerText.includes('Saved!')), false);
  }]);
  // #376: a citation's score is a number (higher is better) or null when Weaviate gave none.
  cases.push(['citations show a numeric score and n/a for a null score', async page => {
    await page.evaluate(() => {
      window.queryConfigFixture.queryResult = {
        answer: 'Fixture answer', retrieval_latency_ms: 1, llm_latency_ms: 1, chunks_retrieved: 2,
        citations: [
          { source_file: 'scored.txt', chunk_index: 3, score: 0.9123, excerpt: 'Scored excerpt' },
          { source_file: 'unscored.txt', chunk_index: 4, score: null, excerpt: 'Unscored excerpt' },
        ],
      };
    });
    assert.equal(await clickByText(page, 'Q&A'), true);
    await page.waitForSelector('textarea');
    await page.evaluate(() => {
      const box = [...document.querySelectorAll('label')].find(l => l.innerText.includes('Show source citations')).querySelector('input');
      if (!box.checked) box.click();
    });
    await setValue(page, '() => document.querySelector("textarea")', 'Which sources?');
    assert.equal(await clickByText(page, 'Ask'), true);
    await page.waitForFunction(() => document.body.innerText.includes('Unscored excerpt') || document.body.innerText.includes('Fixture answer'));
    await sleep(200);
    const text = await page.evaluate(() => document.body.innerText);
    assert.match(text, /chunk 3 \S score 0\.912\b/, 'the numeric score is not shown with 3 decimals');
    assert.match(text, /chunk 4 \S score n\/a/, 'the null score is not shown as n/a');
  }]);
  for (const [name, test] of cases) {
    let s;
    try {
      s = await fixture(browser, base);
      await test(s.page);
      assert.deepEqual(s.errors, []);
      reporter.check(name, true);
    } catch (error) {
      reporter.check(name, false, error.stack || error.message);
    } finally {
      if (s) await s.ctx.close();
    }
  }
}

module.exports = { runQueryConfigTests };

if (require.main === module) {
  (async () => {
    const browser = await require('puppeteer-core').launch({
      executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser',
      headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
    const reporter = makeReporter();
    try { await runQueryConfigTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
    finally { await browser.close(); }
    process.exitCode = reporter.summary() ? 0 : 1;
  })().catch(error => { console.error(error); process.exitCode = 2; });
}
