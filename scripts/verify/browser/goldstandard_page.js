// Gold Standard page details (#393): the default export name, the encoded
// download URL with the popup blocker on, the review row preview and the sample
// size bounds. Every /api/ call from the page is answered here; no backend writes.
const { sleep, setValue, clickByText, bodyText, makeReporter } = require('./lib');

const SESSION = 'gs_0393abcd';
const NAME_RE = /^FixtureGold_\d{8}_\d{6}\.json$/;
// A name the API can't save today, so the check covers every character the
// issue names; the page must encode whatever name the API returns.
const SAVED = 'Gold 393 #1?%.json';
const DOWNLOAD = '/api/goldstandard/download/' + encodeURIComponent(SAVED);
const CONTEXT = 'abcdefghij'.repeat(40); // 400 characters
const LONG = 'L'.repeat(140);

const pair = { pair_id: 'p1', question: 'Fixture question 393', answer: 'Fixture answer', contexts: [CONTEXT], ground_truth: 'Fixture answer', source_file: 'fixture.txt', chunk_index: 0, status: 'approved' };
const sessionBody = collection => ({ session_id: SESSION, collection, status: 'completed', pairs_total: 1, pairs_completed: 1, pairs: [pair] });
const json = (request, status, body) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(body) });

// A page whose /api/ calls are all answered by `opts`; window.open is recorded
// (and opens nothing) unless realOpen is set.
async function fixture(browser, base, opts = {}) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [], generates = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.evaluateOnNewDocument(realOpen => {
    sessionStorage.setItem('rag_role', JSON.stringify({ role: 'engineer' }));
    window.__opened = [];
    if (!realOpen) window.open = url => { window.__opened.push(String(url)); return null; };
  }, !!opts.realOpen);
  await page.setRequestInterception(true);
  page.on('request', async request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return request.continue();
    if (path === '/api/collections') {
      return json(request, 200, { collections: ['FixtureGold', 'Other'].map(name => ({ name, object_count: 1, index_type: 'hnsw', distance_metric: 'cosine' })) });
    }
    if (path === '/api/goldstandard/generate') {
      generates.push(JSON.parse(request.postData() || '{}'));
      return json(request, 200, { session_id: SESSION, status: 'generating', pairs_total: 1 });
    }
    if (path === '/api/goldstandard/session/' + SESSION) return json(request, 200, sessionBody(opts.collection || 'FixtureGold'));
    if (path === '/api/goldstandard/save') {
      if (opts.saveDelay) await sleep(opts.saveDelay);
      return json(request, 200, { filename: SAVED, pairs_saved: 1, pairs_excluded: 0, download_url: '/api/goldstandard/download/' + SAVED, historical: false, session_validity: { stale: false } });
    }
    return json(request, 404, { error: { code: 'NOT_FOUND', message: 'Not stubbed by the fixture.' } });
  });
  await page.goto(base + '/goldstandard', { waitUntil: 'networkidle2' });
  await sleep(300);
  return { ctx, page, errors, generates };
}

const filenameValue = page => page.evaluate(() => document.querySelector('input[placeholder="filename.json"]')?.value ?? null);

async function lookup(page) {
  await setValue(page, '() => document.getElementById("retained-session")', SESSION);
  await sleep(100); await clickByText(page, 'Load / refresh session'); await sleep(400);
}

async function runGoldStandardPageTests(browser, base, r) {
  r.section('Gold Standard page details (#393)');

  async function run(name, opts, body, inBrowser = browser) {
    let s;
    try {
      s = await fixture(inBrowser, base, opts);
      await body(s);
      r.check(name + ': no React page errors', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    } catch (e) {
      r.check(name, false, e.stack || e.message);
    } finally { if (s) await s.ctx.close(); }
  }

  await run('generate', {}, async s => {
    await clickByText(s.page, 'Generate Pairs'); await sleep(2600);
    const value = await filenameValue(s.page);
    r.check('after Generate the filename is {collection}_{YYYYMMDD_HHMMSS}.json, with no double dot', NAME_RE.test(value || ''), String(value));
  });

  await run('lookup', { collection: 'Gold fixture-393' }, async s => {
    await lookup(s.page);
    const value = await filenameValue(s.page);
    r.check('a loaded session pre-fills the filename from its collection, as the API would name it', /^Gold_fixture393_\d{8}_\d{6}\.json$/.test(value || ''), String(value));
  });

  await run('long name', { collection: LONG }, async s => {
    await lookup(s.page);
    const value = await filenameValue(s.page) || '';
    r.check('a long collection name is shortened to a 128-character filename', value.length === 128 && /^L+_\d{8}_\d{6}\.json$/.test(value), `${value.length}: ${value}`);
  });

  await run('download URL', {}, async s => {
    await lookup(s.page);
    await clickByText(s.page, 'Export Approved'); await sleep(500);
    const opened = await s.page.evaluate(() => window.__opened);
    r.check('export opens the download URL with the filename encoded', opened.length === 1 && new URL(opened[0], base).pathname === DOWNLOAD && !opened[0].includes('#'), JSON.stringify(opened));
  });

  await run('review row', {}, async s => {
    await lookup(s.page);
    const row = await s.page.evaluate(() => {
      const d = [...document.querySelectorAll('details')].find(x => x.textContent.includes('Fixture question 393'));
      if (!d) return null;
      const closed = d.open; d.querySelector('summary').click();
      const ctx = [...d.querySelectorAll('div')].find(x => x.textContent.startsWith('Context:'));
      return { closed, open: d.open, preview: ctx ? ctx.querySelector('span').textContent : null };
    });
    r.check('a review row expands to the context: its first 300 characters, then …',
      row && row.closed === false && row.open === true && row.preview === CONTEXT.slice(0, 300) + '…', JSON.stringify(row && { ...row, preview: row.preview && row.preview.length }));
  });

  await run('sample size', {}, async s => {
    const state = () => s.page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Generate Pairs');
      return { disabled: b.disabled, message: document.body.innerText.includes('Sample size must be a whole number from 1 to 100.') };
    });
    for (const bad of ['0', '101', '2.5', '']) {
      await setValue(s.page, '() => document.querySelector("input[type=number]")', bad); await sleep(100);
      await s.page.evaluate(() => [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Generate Pairs').click());
      await sleep(200);
      const st = await state();
      r.check(`sample size "${bad}" disables Generate with the 1-100 message`, st.disabled && st.message, JSON.stringify(st));
    }
    r.check('an out-of-range sample size sends no generate request', s.generates.length === 0, JSON.stringify(s.generates));
    await setValue(s.page, '() => document.querySelector("input[type=number]")', '100'); await sleep(100);
    const ok = await state();
    r.check('sample size 100 enables Generate and removes the message', !ok.disabled && !ok.message, JSON.stringify(ok));
  });

  // Puppeteer starts Chromium with --disable-popup-blocking; turn the blocker back
  // on and click for real, as a user would, so the window.open after the save's
  // await is judged by the browser's user-activation rule.
  const blocking = await require('puppeteer-core').launch({
    executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: 'new',
    args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    ignoreDefaultArgs: ['--disable-popup-blocking'],
  });
  try {
    for (const [delay, wantTab] of [[1000, true], [6000, false]]) {
      await run(`popup blocker, save after ${delay} ms`, { realOpen: true, saveDelay: delay }, async s => {
        await lookup(s.page);
        const tabs = [];
        const onTarget = t => { if (t.type() === 'page' && t.opener()) tabs.push(t.url()); };
        blocking.on('targetcreated', onTarget);
        const button = await s.page.evaluateHandle(() => [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Export Approved'));
        await button.click();
        await sleep(delay + 1500);
        blocking.off('targetcreated', onTarget);
        await s.page.bringToFront();
        const done = /1 pairs exported/.test(await bodyText(s.page));
        if (wantTab) {
          r.check('with the popup blocker on, a save answered in 1 s opens the download tab', done && tabs.length === 1 && new URL(tabs[0]).pathname === DOWNLOAD, JSON.stringify({ done, tabs }));
        } else {
          r.check('control: the blocker is on (a save answered after 6 s opens no tab)', done && tabs.length === 0, JSON.stringify({ done, tabs }));
        }
      }, blocking);
    }
  } finally { await blocking.close(); }
}

module.exports = { runGoldStandardPageTests };

if (require.main === module) {
  (async () => {
    const browser = await require('puppeteer-core').launch({
      executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser',
      headless: 'new', args: ['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
    });
    const reporter = makeReporter();
    try { await runGoldStandardPageTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
    finally { await browser.close(); }
    process.exitCode = reporter.summary() ? 0 : 1;
  })().catch(error => { console.error(error); process.exitCode = 2; });
}
