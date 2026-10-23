// Gold Standard page details (#393): the default export name, the encoded
// download URL with the popup blocker on, the review row preview and the sample
// size bounds, the export name fallbacks, and the popup blocker. Generation outcomes and the guards on actions in flight (#380):
// the final status, failed pairs and reasons, a stopped poll, Generate and
// Regenerate while they run, and a refused edit. Every /api/ call from the page
// is answered here; no backend writes.
const { sleep, setValue, clickByText, bodyText, makeReporter } = require('./lib');

const SESSION = 'gs_0393abcd';
const NAME_RE = /^FixtureGold_\d{8}_\d{6}\.json$/;
// A name the API can't save today, so the check covers every character the
// issue names; the page must encode whatever name the API returns.
const SAVED = 'Gold 393 #1?%.json';
const DOWNLOAD = '/api/goldstandard/download/' + encodeURIComponent(SAVED);
const CONTEXT = 'abcdefghij'.repeat(40); // 400 characters
const LONG = 'L'.repeat(140);
const ANSWER = 'Distinct answer 469';
const TRUTH = 'Distinct ground truth 469';

const pair = { pair_id: 'p1', question: 'Fixture question 393', answer: 'Fixture answer', contexts: [CONTEXT], ground_truth: 'Fixture answer', source_file: 'fixture.txt', chunk_index: 0, status: 'approved' };
const sessionBody = (collection, p = pair) => ({ session_id: SESSION, collection, status: 'completed', pairs_total: 1, pairs_completed: 1, pairs: [p] });
const json = (request, status, body) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(body) });
const apiError = (status, message) => ({ status, body: { error: { code: 'FIXTURE', message, detail: null } } });
const pair2 = { ...pair, pair_id: 'p2', question: 'Second question 380', status: 'pending' };

// A page whose /api/ calls are all answered by `opts`; window.open is recorded
// (and opens nothing) unless realOpen is set.
async function fixture(browser, base, opts = {}) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const activation = {}, errors = [], generates = [], polls = [], patches = [], regenerates = [], dialogs = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', async d => { dialogs.push(d.message()); await d.dismiss(); });
  await page.evaluateOnNewDocument(realOpen => {
    sessionStorage.setItem('rag_role', JSON.stringify({ role: 'engineer' }));
    window.__opened = [];
    if (!realOpen) window.open = url => { window.__opened.push(String(url)); return null; };
  }, !!opts.realOpen);
  if (opts.pollMs) {
    // The page polls every 2 s; a case that needs more polls asks for a shorter delay.
    await page.evaluateOnNewDocument(ms => {
      const si = window.setInterval.bind(window);
      window.setInterval = (fn, delay, ...rest) => si(fn, delay === 2000 ? ms : delay, ...rest);
    }, opts.pollMs);
  }
  await page.setRequestInterception(true);
  page.on('request', async request => {
    const path = new URL(request.url()).pathname;
    if (!path.startsWith('/api/')) return request.continue();
    if (path === '/api/collections') {
      return json(request, 200, { collections: ['FixtureGold', 'Other'].map(name => ({ name, object_count: 1, index_type: 'hnsw', distance_metric: 'cosine' })) });
    }
    if (path === '/api/goldstandard/generate') {
      generates.push(JSON.parse(request.postData() || '{}'));
      if (opts.generateDelay) await sleep(opts.generateDelay);
      return json(request, 200, { session_id: SESSION, status: 'generating', pairs_total: 1 });
    }
    if (path === '/api/goldstandard/session/' + SESSION && request.method() === 'GET') {
      polls.push(Date.now());
      const answer = opts.session ? opts.session(polls.length) : null;
      if (answer) return json(request, answer.status || 200, answer.body);
      return json(request, 200, sessionBody(opts.collection || 'FixtureGold'));
    }
    if (path.startsWith('/api/goldstandard/session/' + SESSION + '/pair/') && request.method() === 'PATCH') {
      const body = JSON.parse(request.postData() || '{}');
      patches.push(body);
      const answer = opts.patch ? opts.patch(patches.length) : null;
      if (answer) return json(request, answer.status, answer.body);
      const target = [pair, pair2].find(x => x.pair_id === path.split('/').pop());
      return json(request, 200, { ...target, ...body });
    }
    if (path === '/api/goldstandard/regenerate') {
      const body = JSON.parse(request.postData() || '{}');
      regenerates.push(body);
      if (opts.regenerateDelay) await sleep(opts.regenerateDelay);
      return json(request, 200, { ...[pair, pair2].find(x => x.pair_id === body.pair_id), question: 'Regenerated question 380', status: 'pending' });
    }
    if (path === '/api/goldstandard/save') {
      if (opts.saveDelay) await sleep(opts.saveDelay);
      if (opts.saveUntilInactive) {
        // Hold the answer until the browser says the click's user activation has
        // lapsed, not for a fixed time; give up after 15 s. page.evaluate would
        // itself count as a user gesture and keep the activation alive, so ask
        // through the protocol with userGesture off.
        const cdp = await page.createCDPSession();
        const isActive = () => cdp.send('Runtime.evaluate', { expression: 'navigator.userActivation.isActive', returnByValue: true, userGesture: false })
          .then(x => x.result.value === true, () => false);
        activation.atRequest = await isActive();
        for (let i = 0; i < 150 && await isActive(); i++) await sleep(100);
        activation.lapsed = !(await isActive());
      }
      return json(request, 200, { filename: SAVED, pairs_saved: 1, pairs_excluded: 0, download_url: '/api/goldstandard/download/' + SAVED, historical: false, session_validity: { stale: false } });
    }
    return json(request, 404, { error: { code: 'NOT_FOUND', message: 'Not stubbed by the fixture.' } });
  });
  await page.goto(base + '/goldstandard', { waitUntil: 'networkidle2' });
  await sleep(300);
  return { ctx, page, activation, errors, generates, polls, patches, regenerates, dialogs };
}

const filenameValue = page => page.evaluate(() => document.querySelector('input[placeholder="filename.json"]')?.value ?? null);

async function lookup(page) {
  await setValue(page, '() => document.getElementById("retained-session")', SESSION);
  await sleep(100); await clickByText(page, 'Load / refresh session'); await sleep(400);
}

// Polls the page until fn(arg) is true, for up to 8 s.
async function until(page, fn, arg) {
  for (let i = 0; i < 80; i++) {
    if (await page.evaluate(fn, arg)) return true;
    await sleep(100);
  }
  return false;
}
const button = (page, text) => page.evaluate(t => {
  const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === t);
  return b ? { disabled: b.disabled } : null;
}, text);
const generateState = page => button(page, 'Generate Pairs');
// A row's action button, found by the row's question and the button's title.
const rowButton = (page, question, title) => page.evaluate((q, t) => {
  const d = [...document.querySelectorAll('details')].find(x => x.querySelector('summary .font-medium')?.textContent.trim() === q);
  const b = d && [...d.querySelectorAll('summary button')].find(x => x.title === t || x.title === 'Regenerating...' && t === 'Regenerate');
  return b ? { disabled: b.disabled, title: b.title } : null;
}, question, title);
const clickRow = (page, question, title) => page.evaluate((q, t) => {
  const d = [...document.querySelectorAll('details')].find(x => x.querySelector('summary .font-medium')?.textContent.trim() === q);
  const b = d && [...d.querySelectorAll('summary button')].find(x => x.title === t || x.title === 'Regenerating...' && t === 'Regenerate');
  if (!b) return false; b.click(); return true;
}, question, title);
const page_modalAlert = page => page.evaluate(() => document.querySelector('.fixed [role=alert]')?.textContent.trim() ?? null);
const alerts = page => page.evaluate(() => [...document.querySelectorAll('[role=alert]')].map(x => x.textContent.replace(/\s+/g, ' ').trim()));
const fields = page => page.evaluate(() => [...document.querySelectorAll('.fixed textarea')].map(t => t.value));

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

  await run('no collection', { session: () => ({ body: { ...sessionBody('x'), collection: undefined } }) }, async s => {
    await lookup(s.page);
    const value = await filenameValue(s.page);
    r.check('a session with no collection pre-fills export_{YYYYMMDD_HHMMSS}.json', /^export_\d{8}_\d{6}\.json$/.test(value || ''), String(value));
  });

  await run('no safe characters', { collection: '#?%!' }, async s => {
    await lookup(s.page);
    const value = await filenameValue(s.page);
    r.check('a collection with no safe characters pre-fills _{YYYYMMDD_HHMMSS}.json, as the API names it', /^_\d{8}_\d{6}\.json$/.test(value || ''), String(value));
  });

  await run('download URL', {}, async s => {
    await lookup(s.page);
    await clickByText(s.page, 'Export Approved'); await sleep(500);
    const opened = await s.page.evaluate(() => window.__opened);
    r.check('export opens the download URL with the filename encoded', opened.length === 1 && new URL(opened[0], base).pathname === DOWNLOAD && !opened[0].includes('#'), JSON.stringify(opened));
  });

  await run('review row', { session: () => ({ body: sessionBody('FixtureGold', { ...pair, answer: ANSWER, ground_truth: TRUTH }) }) }, async s => {
    await lookup(s.page);
    const row = await s.page.evaluate(() => {
      const d = [...document.querySelectorAll('details')].find(x => x.textContent.includes('Fixture question 393'));
      if (!d) return null;
      const closed = d.open; d.querySelector('summary').click();
      const ctx = [...d.querySelectorAll('div')].find(x => x.textContent.startsWith('Context:'));
      const field = label => {
        const x = [...d.querySelectorAll('div')].find(y => y.firstElementChild?.tagName === 'STRONG' && y.firstElementChild.textContent === label);
        return x ? x.textContent.slice(label.length).trim() : null;
      };
      return { closed, open: d.open, preview: ctx ? ctx.querySelector('span').textContent : null, answer: field('Answer:'), truth: field('Ground Truth:') };
    });
    r.check('a review row expands to the context: its first 300 characters, then …',
      row && row.closed === false && row.open === true && row.preview === CONTEXT.slice(0, 300) + '…', JSON.stringify(row && { ...row, preview: row.preview && row.preview.length }));
    r.check('a review row expands to its answer', row && row.answer === ANSWER, JSON.stringify(row && row.answer));
    r.check('a review row expands to its ground truth, apart from the answer', row && row.truth === TRUTH, JSON.stringify(row && row.truth));
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
    await setValue(s.page, '() => document.querySelector("input[type=number]")', '1'); await sleep(100);
    const low = await state();
    r.check('sample size 1 enables Generate and removes the message', !low.disabled && !low.message, JSON.stringify(low));
  });

  r.section('Gold Standard outcomes and in-flight guards (#380)');
  const settled = over => ({ ...sessionBody('FixtureGold'), pairs_attempted: 1, pairs_failed: 0, errors: [], ...over });
  const distinct = n => Array.from({ length: n }, (_, i) => `PAIR_GENERATION_FAILED: reason ${i + 1}`);

  const outcomes = [
    ['completed', settled({ pairs_total: 3, pairs_completed: 3 }),
      ['Generation completed: 3 of 3 pairs generated.'], ['failed', 'no chunks'], false],
    ['completed with failed pairs', settled({ pairs_total: 4, pairs_completed: 1, pairs_failed: 3, errors: ['TimeoutError: slow model', 'TimeoutError: slow model', 'ValueError: bad json'] }),
      ['Generation completed: 1 of 4 pairs generated.', '3 pairs failed and are missing from the review list.', 'TimeoutError: slow model (x2)', 'ValueError: bad json'], ['no chunks'], true],
    ['failed', settled({ status: 'failed', pairs_total: 7, pairs_completed: 0, pairs_failed: 7, pairs: [], errors: distinct(7) }),
      ['Generation failed.', '7 of 7 pairs failed.', 'reason 1', 'reason 5', 'and 2 more reasons'], ['reason 6', 'Generation completed'], true],
    ['cancelled', settled({ status: 'cancelled', pairs_total: 5, pairs_completed: 0, pairs_failed: 0, pairs: [] }),
      ['Generation was cancelled.'], ['Generation completed'], true],
    ['empty collection', settled({ pairs_total: 0, pairs_completed: 0, pairs: [] }),
      ['No pairs were generated: the collection has no chunks.'], ['Generation failed'], true],
  ];
  for (const [name, body, want, notWant, isAlert] of outcomes) {
    await run('outcome ' + name, { session: () => ({ body }) }, async s => {
      await lookup(s.page);
      const text = await bodyText(s.page);
      const missing = want.filter(w => !text.includes(w)), extra = notWant.filter(w => text.includes(w));
      r.check(`outcome, ${name}: the page shows it`, missing.length === 0 && extra.length === 0, JSON.stringify({ missing, extra, text: text.slice(0, 300) }));
      const roles = await alerts(s.page);
      r.check(`outcome, ${name}: it is ${isAlert ? 'an alert' : 'a status, not an alert'}`, roles.some(a => a.includes(want[0])) === isAlert, JSON.stringify(roles));
    });
  }

  await run('generate guard', {
    pollMs: 700, generateDelay: 100,
    session: n => ({ body: n < 3 ? settled({ status: 'generating', pairs_total: 2, pairs_attempted: 0, pairs_completed: 0, pairs: [] }) : settled({ pairs_total: 2, pairs_completed: 2 }) }),
  }, async s => {
    await clickByText(s.page, 'Generate Pairs');
    await sleep(350); // the start is answered; the first poll is still due
    r.check('Generate is disabled between the start being accepted and the first poll', (await generateState(s.page)).disabled === true, JSON.stringify(await generateState(s.page)));
    await s.page.evaluate(() => [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Generate Pairs').click());
    await sleep(100);
    r.check('a click in that window starts no second session and asks no confirm', s.generates.length === 1 && s.dialogs.length === 0, JSON.stringify({ generates: s.generates.length, dialogs: s.dialogs }));
    r.check('while the session is generating the progress shows and Generate stays disabled', await until(s.page, () => document.body.innerText.includes('Generating') && [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Generate Pairs').disabled));
    r.check('when it completes the outcome shows and Generate is enabled again', await until(s.page, () => document.body.innerText.includes('Generation completed: 2 of 2 pairs generated.') && ![...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Generate Pairs').disabled), JSON.stringify(await generateState(s.page)));
  });

  await run('generating session loaded by id', {
    session: () => ({ body: settled({ status: 'generating', pairs_total: 2, pairs_attempted: 0, pairs_completed: 0, pairs: [] }) }),
  }, async s => {
    await lookup(s.page);
    r.check('a retained session still generating disables Generate', (await generateState(s.page)).disabled === true, JSON.stringify(await generateState(s.page)));
  });

  for (const [label, failure] of [['an outage', apiError(503, 'Synthetic outage.')], ['a missing session', apiError(404, 'Synthetic missing session.')]]) {
    const ctl = { fail: true };
    await run('poll error, ' + label, {
      pollMs: 150,
      session: n => (n >= 2 && ctl.fail) ? failure : { body: n >= 2 ? settled({ pairs_total: 2, pairs_completed: 2 }) : settled({ status: 'generating', pairs_total: 2, pairs_attempted: 0, pairs_completed: 0, pairs: [] }) },
    }, async s => {
      await lookup(s.page);
      const shown = await until(s.page, () => document.body.innerText.includes('Could not read the generation'));
      const message = (await alerts(s.page)).find(a => a.includes('Could not read the generation')) || '';
      r.check(`${label} while polling is shown with its reason and the way back`, shown && message.includes(failure.body.error.message) && message.includes('Polling stopped; use Load / refresh session to check again.'), message);
      const count = s.polls.length;
      await sleep(900);
      r.check(`${label} stops the polling`, s.polls.length === count, `${count} -> ${s.polls.length}`);
      r.check(`${label} releases Generate and drops the stale progress`, (await generateState(s.page)).disabled === false && !(await bodyText(s.page)).includes('Generating'), JSON.stringify(await generateState(s.page)));
      ctl.fail = false;
      await clickByText(s.page, 'Load / refresh session');
      r.check(`Load / refresh after ${label} clears the message and shows the outcome`, await until(s.page, () => document.body.innerText.includes('Generation completed: 2 of 2 pairs generated.') && !document.body.innerText.includes('Could not read the generation')));
    });
  }

  await run('regenerate guard', {
    regenerateDelay: 700, session: () => ({ body: settled({ pairs_total: 2, pairs_completed: 2, pairs: [pair, pair2] }) }),
  }, async s => {
    await lookup(s.page);
    await clickRow(s.page, pair.question, 'Regenerate');
    await sleep(150);
    await clickRow(s.page, pair.question, 'Regenerate');
    const busy = await rowButton(s.page, pair.question, 'Regenerate'), other = await rowButton(s.page, pair2.question, 'Regenerate');
    r.check('while a pair regenerates its Regenerate button is disabled and says so', busy && busy.disabled && busy.title === 'Regenerating...', JSON.stringify(busy));
    r.check('another pair\'s Regenerate button stays enabled', other && !other.disabled, JSON.stringify(other));
    await sleep(100);
    r.check('repeated clicks send one regenerate request', s.regenerates.length === 1, JSON.stringify(s.regenerates));
    r.check('when it ends the pair is replaced and its button is enabled again', await until(s.page, () => document.body.innerText.includes('Regenerated question 380')) && (await rowButton(s.page, 'Regenerated question 380', 'Regenerate')).disabled === false);
  });

  await run('refused edit', {
    session: () => ({ body: settled({ pairs_total: 2, pairs_completed: 2, pairs: [pair, pair2] }) }),
    patch: n => n === 1 ? apiError(422, 'Request parameters are invalid.') : null,
  }, async s => {
    await lookup(s.page);
    await clickRow(s.page, pair.question, 'Edit');
    await sleep(200);
    await setValue(s.page, '() => document.querySelectorAll(".fixed textarea")[0]', 'Typed question 380');
    await setValue(s.page, '() => document.querySelectorAll(".fixed textarea")[1]', '');
    await clickByText(s.page, 'Save'); await sleep(500);
    const open = (await bodyText(s.page)).includes('Edit Pair');
    r.check('a refused edit keeps the modal open with the typed values', open && JSON.stringify(await fields(s.page)) === JSON.stringify(['Typed question 380', '', pair.ground_truth]), JSON.stringify(await fields(s.page)));
    const inModal = await page_modalAlert(s.page);
    r.check('the refusal is shown inside the modal', inModal === 'Request parameters are invalid.', String(inModal));
    r.check('a refused edit sets no page-level error', await s.page.evaluate(() => ![...document.querySelectorAll('p.text-red-600')].some(x => !x.closest('.fixed'))));
    r.check('the first edit sent only the changed fields', JSON.stringify(s.patches[0]) === JSON.stringify({ status: 'edited', question: 'Typed question 380', answer: '' }), JSON.stringify(s.patches));
    await setValue(s.page, '() => document.querySelectorAll(".fixed textarea")[1]', 'Typed answer 380');
    await clickByText(s.page, 'Save');
    r.check('a retry with the typed values saves, closes the modal and updates the row', await until(s.page, () => !document.body.innerText.includes('Edit Pair') && document.body.innerText.includes('Typed question 380')));
    r.check('the retry sent the changed fields only', JSON.stringify(s.patches[1]) === JSON.stringify({ status: 'edited', question: 'Typed question 380', answer: 'Typed answer 380' }), JSON.stringify(s.patches));
  });

  await run('edit of one field', {
    session: () => ({ body: settled({ pairs_total: 2, pairs_completed: 2, pairs: [pair, pair2] }) }),
  }, async s => {
    await lookup(s.page);
    await clickRow(s.page, pair2.question, 'Edit');
    await sleep(200);
    await setValue(s.page, '() => document.querySelectorAll(".fixed textarea")[2]', 'Only the truth changed');
    await clickByText(s.page, 'Save');
    await until(s.page, () => !document.body.innerText.includes('Edit Pair'));
    r.check('an edit of one field sends that field and the status only', JSON.stringify(s.patches) === JSON.stringify([{ status: 'edited', ground_truth: 'Only the truth changed' }]), JSON.stringify(s.patches));
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
    for (const [label, wantTab, extra] of [['1 s', true, { saveDelay: 1000 }], ['the click\'s user activation has lapsed', false, { saveUntilInactive: true }]]) {
      await run(`popup blocker, save after ${label}`, { realOpen: true, ...extra }, async s => {
        await lookup(s.page);
        const tabs = [];
        const onTarget = t => { if (t.type() === 'page' && t.opener()) tabs.push(t.url()); };
        blocking.on('targetcreated', onTarget);
        const button = await s.page.evaluateHandle(() => [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Export Approved'));
        await button.click();
        // Wait without page.evaluate, which counts as a user gesture and would keep the activation alive.
        if (extra.saveUntilInactive) for (let i = 0; i < 200 && s.activation.lapsed === undefined; i++) await sleep(100);
        await sleep(extra.saveUntilInactive ? 1500 : 2500);
        blocking.off('targetcreated', onTarget);
        await s.page.bringToFront();
        const done = /1 pairs exported/.test(await bodyText(s.page));
        if (wantTab) {
          r.check('with the popup blocker on, a save answered in 1 s opens the download tab', done && tabs.length === 1 && new URL(tabs[0]).pathname === DOWNLOAD, JSON.stringify({ done, tabs }));
        } else {
          r.check('control: the blocker is on (a save answered once the user activation has lapsed opens no tab)', done && tabs.length === 0 && s.activation.atRequest === true && s.activation.lapsed === true, JSON.stringify({ done, tabs, activation: s.activation }));
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
