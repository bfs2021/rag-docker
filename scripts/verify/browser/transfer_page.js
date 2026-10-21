// Transfer page polling and failure display (#382), against the rendered page
// with every API call stubbed in the browser; no backend reads or writes.
//
// Each case gets fresh fetch stubs. Fixed answers cover the collections, the
// package list and the fidelity lookup unless a case holds them; every other
// /api/ request is held until the case releases it with a status and body. The
// page's 2 s poll delay is cut to 20 ms, so 30 failed polls take well under a
// second.
const assert = require('node:assert/strict');
const { makeReporter, setValue } = require('./lib');

const A = 'VfyTransferA', B = 'VfyTransferB';
const PACKAGE = 'ragpkg-vfytransfera-20261021-0000-abcd1234.tar.gz';
const collectionsBody = { collections: [A, B].map(name => ({ name, object_count: 3, index_type: 'hnsw', distance_metric: 'cosine', created_at: null })) };
const packagesBody = { packages: [{ filename: PACKAGE, size_bytes: 2048, collection: A, chunk_count: 3, fidelity: 'with-sources', created_at: null, readable: true }] };
const tuneBody = name => ({ collection: name, fidelity: 'with-sources', source_document_count: 2, can_rechunk: true, can_reembed: true, can_reindex: true, note: '' });
const json = (status, body) => ({ status, body: JSON.stringify(body) });
const apiError = (status, code, msg) => json(status, { error: { code, message: msg, detail: null } });
// nginx's own page when the API is down; request() turns it into this message.
const BAD_GATEWAY = { status: 502, body: '<html><head><title>502 Bad Gateway</title></head><body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body></html>' };
const DOWN = 'The API is not responding. It may still be starting; try again in a minute.';
const exportJob = (status, extra = {}) => json(200, { job_id: 'vfyexp', status, collection: A, chunks_written: 1, filename: null, size_bytes: null, source_document_count: null, fidelity: null, models_bundled: null, retrieve_script: null, warnings: [], error: null, error_code: null, error_detail: null, ...extra });
const importJob = (status, extra = {}) => json(200, { job_id: 'vfyimp', status, filename: PACKAGE, on_conflict: 'abort', collection: null, original_collection: null, chunks_written: 1, fidelity: null, renamed: false, notes: [], error: null, error_code: null, error_detail: null, ...extra });

const HALVES = {
  export: { start: '/api/export', started: json(202, { job_id: 'vfyexp', status: 'queued', collection: A }), poll: '/api/export/job/vfyexp', job: exportJob, idle: 'Export', busy: 'Exporting…', progress: "the export's progress",
            done: exportJob('completed', { chunks_written: 3, filename: 'ragpkg-vfy.tar.gz', size_bytes: 4096, fidelity: 'with-sources', models_bundled: false, retrieve_script: true }) },
  import: { start: '/api/import', started: json(202, { job_id: 'vfyimp', status: 'queued', filename: PACKAGE }), poll: '/api/import/job/vfyimp', job: importJob, idle: 'Import', busy: 'Importing…', progress: "the import's progress",
            done: importJob('completed', { collection: A, chunks_written: 3, fidelity: 'with-sources' }) },
};

async function fixture(browser, base, { hold = [] } = {}) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const fixed = {
    'GET /api/collections': json(200, collectionsBody),
    'GET /api/packages': json(200, packagesBody),
    ['GET /api/tune/' + A]: json(200, tuneBody(A)),
    ['GET /api/tune/' + B]: json(200, tuneBody(B)),
  };
  for (const key of hold) delete fixed[key];
  await page.evaluateOnNewDocument(answers => {
    sessionStorage.setItem('rag_role', JSON.stringify({ role: 'engineer' }));
    const realTimeout = window.setTimeout.bind(window);
    window.setTimeout = (fn, ms, ...args) => realTimeout(fn, ms === 2000 ? 20 : ms, ...args);
    const realFetch = window.fetch.bind(window);
    window.transferFixture = { requests: [] };
    window.fetch = (input, init = {}) => {
      const path = new URL(input, location.href).pathname;
      if (!path.startsWith('/api/')) return realFetch(input, init);
      const method = init.method || 'GET';
      const entry = { path, method, done: false };
      window.transferFixture.requests.push(entry);
      const answer = answers[method + ' ' + path];
      if (answer) { entry.done = true; return Promise.resolve(new Response(answer.body, { status: answer.status })); }
      return new Promise(resolve => { entry.resolve = (status, body) => { entry.done = true; resolve(new Response(body, { status })); }; });
    };
  }, fixed);
  await page.goto(base + '/transfer', { waitUntil: 'domcontentloaded' });
  await page.waitForFunction(() => document.querySelectorAll('select option').length > 0 && document.body.innerText.includes('Export a collection'));
  return { ctx, page, errors };
}

const count = (page, path, method = 'GET') => page.evaluate((p, m) => window.transferFixture.requests.filter(r => r.path === p && r.method === m).length, path, method);
async function pending(page, path, method = 'GET') {
  await page.waitForFunction((p, m) => window.transferFixture.requests.some(r => r.path === p && r.method === m && !r.done), { timeout: 5000 }, path, method)
    .catch(() => { throw new Error(`no ${method} ${path} request arrived`); });
}
async function release(page, path, answer, method = 'GET') {
  await pending(page, path, method);
  await page.evaluate((p, m, status, body) => {
    window.transferFixture.requests.find(r => r.path === p && r.method === m && !r.done).resolve(status, body);
  }, path, method, answer.status, answer.body);
}
const text = page => page.evaluate(() => document.body.innerText.replace(/\s+/g, ' '));
async function waitText(page, want, gone = false) {
  await page.waitForFunction((w, g) => document.body.innerText.replace(/\s+/g, ' ').includes(w) !== g, { timeout: 5000 }, want, gone)
    .catch(async () => { throw new Error(`${gone ? 'still shows' : 'never showed'} "${want}"; page: ${(await text(page)).slice(0, 600)}`); });
}
// The half's start button, found by either of its labels.
const button = (page, half) => page.evaluate((idle, busy) => {
  const b = [...document.querySelectorAll('button')].find(x => [idle, busy].includes(x.textContent.trim()));
  return b ? { label: b.textContent.trim(), disabled: b.disabled } : null;
}, HALVES[half].idle, HALVES[half].busy);
async function expectButton(page, half, busy) {
  const want = busy ? HALVES[half].busy : HALVES[half].idle;
  await page.waitForFunction((label, disabled) => [...document.querySelectorAll('button')].some(x => x.textContent.trim() === label && x.disabled === disabled), { timeout: 5000 }, want, busy)
    .catch(async () => { throw new Error(`expected "${want}" ${busy ? 'disabled' : 'enabled'}, got ${JSON.stringify(await button(page, half))}`); });
}
// Buttons only by default: the nav has an "Import" link as well as the Import button.
const click = (page, label, selector = 'button') => page.evaluate((t, sel) => {
  const b = [...document.querySelectorAll(sel)].find(x => x.textContent.trim() === t);
  if (!b) return false; b.click(); return true;
}, label, selector);

// Selects the package (import) and starts the half; leaves its start request held.
async function start(page, half) {
  if (half === 'import') {
    await setValue(page, `() => [...document.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === ${JSON.stringify(PACKAGE)}))`, PACKAGE);
  }
  await expectButton(page, half, false);
  assert.equal(await click(page, HALVES[half].idle), true, `no ${HALVES[half].idle} button`);
  await pending(page, HALVES[half].start, 'POST');
}
async function startAndPoll(page, half) {
  await start(page, half);
  await release(page, HALVES[half].start, HALVES[half].started, 'POST');
  await pending(page, HALVES[half].poll);
}
const retrying = half => `Could not check ${HALVES[half].progress}, retrying: ${DOWN}`;
const stopped = half => `Stopped checking ${HALVES[half].progress} after 30 failed attempts; the ${half} may still be running. Last error: ${DOWN}`;

async function sleep(ms) { await new Promise(r => setTimeout(r, ms)); }

async function runTransferPageTests(browser, base, reporter) {
  reporter.section('Transfer page polling and failures (#382)');
  const cases = [];
  for (const half of ['export', 'import']) {
    const h = HALVES[half];
    cases.push([`${half}: a failed poll is shown and retried with the button still busy, and the next good poll clears it`, async page => {
      await startAndPoll(page, half);
      await release(page, h.poll, BAD_GATEWAY);
      await waitText(page, retrying(half));
      await expectButton(page, half, true);
      await release(page, h.poll, h.job('running'));
      await waitText(page, retrying(half), true);
      await release(page, h.poll, BAD_GATEWAY);
      await waitText(page, retrying(half));
      await expectButton(page, half, true);
      await release(page, h.poll, h.done);
      await waitText(page, 'Done.');
      await waitText(page, retrying(half), true);
      await expectButton(page, half, false);
    }]);
    cases.push([`${half}: after 30 failed polls in a row polling stops, the reason is shown and the button is usable; a new start clears it`, async page => {
      await startAndPoll(page, half);
      await release(page, h.poll, h.job('running'));
      for (let i = 0; i < 30; i++) await release(page, h.poll, BAD_GATEWAY);
      await waitText(page, stopped(half));
      await expectButton(page, half, false);
      const polls = await count(page, h.poll);
      await sleep(400);
      assert.equal(await count(page, h.poll), polls, 'polling went on after the limit');
      assert.equal(polls, 31, 'one good poll and 30 failed ones');
      await start(page, half);
      await waitText(page, stopped(half), true);
    }]);
    cases.push([`${half}: a poll in flight when the page is left schedules no further poll`, async page => {
      await startAndPoll(page, half);
      await release(page, h.poll, h.job('running'));
      await pending(page, h.poll);
      assert.equal(await click(page, 'Collections', 'a'), true, 'no Collections link');
      await page.waitForFunction(() => location.pathname === '/collections');
      await release(page, h.poll, h.job('running'));
      await sleep(400);
      assert.equal(await count(page, h.poll), 2, 'a poll was sent after the page was left');
    }]);
  }
  cases.push(['export: a start answered after the page was left starts no polling', async page => {
    await start(page, 'export');
    assert.equal(await click(page, 'Collections', 'a'), true, 'no Collections link');
    await page.waitForFunction(() => location.pathname === '/collections');
    await release(page, HALVES.export.start, HALVES.export.started, 'POST');
    await sleep(400);
    assert.equal(await count(page, HALVES.export.poll), 0, 'a poll was sent after the page was left');
  }]);
  for (const [status, code, msg] of [[409, 'SOURCE_INDEX_INVALID', 'Retained source index is invalid.'],
                                     [503, 'SERVICE_UNAVAILABLE', 'Synthetic tune options failure.'],
                                     [404, 'COLLECTION_NOT_FOUND', `Collection '${A}' not found.`]]) {
    cases.push([`fidelity: a ${status} ${code} from /tune is labelled, not hidden`, async page => {
      await pending(page, '/api/tune/' + A);
      await waitText(page, 'Fidelity: reading…');
      await release(page, '/api/tune/' + A, apiError(status, code, msg));
      await waitText(page, `Fidelity: unknown — it could not be read: ${msg}`);
    }, { hold: ['GET /api/tune/' + A] }]);
  }
  cases.push(['fidelity: a good lookup shows the fidelity line', async page => {
    await waitText(page, 'Fidelity: with-sources — 2 original document(s) will travel with the package.');
  }]);
  cases.push(['fidelity: a late answer for a collection no longer selected is ignored', async page => {
    await pending(page, '/api/tune/' + A);
    await setValue(page, `() => [...document.querySelectorAll('select')].find(s => [...s.options].some(o => o.value === ${JSON.stringify(B)}))`, B);
    await release(page, '/api/tune/' + B, json(200, { ...tuneBody(B), fidelity: 'chunks-only', source_document_count: 0 }));
    await waitText(page, 'Fidelity: chunks-only');
    await release(page, '/api/tune/' + A, apiError(503, 'SERVICE_UNAVAILABLE', 'Synthetic late failure.'));
    await sleep(300);
    const shown = await text(page);
    assert.ok(shown.includes('Fidelity: chunks-only') && !shown.includes('Synthetic late failure.'), shown.slice(0, 600));
  }, { hold: ['GET /api/tune/' + A, 'GET /api/tune/' + B] }]);
  cases.push(['export: a refused export shows its error_code', async page => {
    await startAndPoll(page, 'export');
    await release(page, HALVES.export.poll, exportJob('failed', { error: 'PackageError: Synthetic embedding model mismatch.', error_code: 'EMBEDDING_MISMATCH', error_detail: { expected: 'a', found: 'b' } }));
    await waitText(page, 'Failed.');
    const codes = await page.evaluate(() => [...document.querySelectorAll('code')].map(c => c.textContent.trim()));
    assert.ok(codes.includes('EMBEDDING_MISMATCH'), `codes shown: ${JSON.stringify(codes)}`);
    await waitText(page, 'Synthetic embedding model mismatch.');
    await expectButton(page, 'export', false);
  }]);
  cases.push(['packages: a failed list is shown without the empty-folder hint, and a good Refresh clears it', async page => {
    await release(page, '/api/packages', apiError(500, 'INTERNAL_ERROR', 'Synthetic package list failure.'));
    await waitText(page, 'Could not list the packages in ./exports: Synthetic package list failure.');
    assert.ok(!(await text(page)).includes('Nothing in ./exports yet'), 'the empty-folder hint is shown for a failed list');
    assert.equal(await click(page, 'Refresh'), true, 'no Refresh button');
    await release(page, '/api/packages', json(200, packagesBody));
    await waitText(page, 'Could not list the packages', true);
    const listed = await page.evaluate(p => [...document.querySelectorAll('option')].some(o => o.value === p), PACKAGE);
    assert.equal(listed, true, 'the package is not listed after a good Refresh');
  }, { hold: ['GET /api/packages'] }]);

  for (const [name, test, opts] of cases) {
    let s;
    try { s = await fixture(browser, base, opts); await test(s.page); assert.deepEqual(s.errors, []); reporter.check(name, true); }
    catch (e) { reporter.check(name, false, e.message); }
    finally { if (s) await s.ctx.close(); }
  }
}

module.exports = { runTransferPageTests };
if (require.main === module) (async () => {
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runTransferPageTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
