// SPECIFICATIONS.md §10.4 — Web UI, plus the Transfer and help pages.
//
// Selectors here are deliberately precise. Loose ones have produced false
// results in both directions: a collections dropdown once matched as the
// chunking-strategy selector, `input[type=text]` missed an input with no type
// attribute, and a row's delete link matched a class test meant for the modal's
// confirm button.
const { sleep, makeReporter, launch, session, bodyText, clickByText } = require('./lib');

const BASE = process.env.RAG_UI_BASE || 'http://proxy';
const STRATEGIES = ['fixed', 'overlap', 'language', 'context_aware', 'semantic'];

(async () => {
  const browser = await launch();
  const r = makeReporter();
  await require('./chunking_config').runChunkingConfigTests(browser, BASE, r);

  await require('./query_config').runQueryConfigTests(browser, BASE, r);

  // ── role persistence ───────────────────────────────────────────────────────
  r.section('§10.4 role selection');
  {
    const s = await session(browser, BASE, null);
    await s.page.goto(BASE + '/', { waitUntil: 'networkidle2' });
    await sleep(1200);
    const picked = await s.page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => /Engineer/i.test(x.textContent));
      if (!b) return false; b.click(); return true;
    });
    r.check('a role can be chosen on the landing page', picked);
    const stored = await s.page.evaluate(() => sessionStorage.getItem('rag_role'));
    r.check('the choice is persisted', !!stored, String(stored));
    for (const p of ['/qa', '/collections', '/health', '/qa']) {
      await s.page.goto(BASE + p, { waitUntil: 'networkidle2' }); await sleep(700);
    }
    const after = await s.page.evaluate(() => sessionStorage.getItem('rag_role'));
    const navPresent = await s.page.evaluate(() => document.querySelectorAll('nav a').length > 0);
    r.check('the role survives navigation', after === stored && navPresent);
    await s.ctx.close();
  }

  // ── owned historical UI fixtures (no persistent backend state) ────────────
  r.section('retained-session UI boundaries');
  {
    const s = await session(browser, BASE, 'engineer');
    const suffix = require('crypto').randomBytes(4).toString('hex');
    const emptyId = 'gs_' + suffix, currentId = 'gs_' + require('crypto').randomBytes(4).toString('hex');
    const missingId = 'gs_' + require('crypto').randomBytes(4).toString('hex');
    const histId = 'gs_' + require('crypto').randomBytes(4).toString('hex');
    const histBodies = [];
    const common = { collection: 'OwnedHistoricalBrowserFixture', status: 'completed', pairs_total: 0, pairs_completed: 0, pairs: [] };
    let exports = 0;
    await s.page.setRequestInterception(true);
    s.page.on('request', async request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/goldstandard/session/' + emptyId) {
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...common, session_id: emptyId, stale: true, stale_reason: 'Synthetic empty history', stale_at: '2026-09-28T00:00:00+00:00', orphaned: true, orphaned_reason: 'Synthetic deleted fixture', orphaned_at: '2026-09-28T00:01:00+00:00' }) });
      }
      if (path === '/api/goldstandard/session/' + currentId) {
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...common, session_id: currentId, pairs_total: 1, pairs_completed: 1, pairs: [{ pair_id: 'fixture', question: 'Owned synthetic question', answer: 'Inert', contexts: ['Inert'], ground_truth: 'Inert', source_file: 'inert.txt', chunk_index: 0, status: 'approved' }] }) });
      }
      if (path === '/api/goldstandard/session/' + missingId) {
        return request.respond({ status: 404, contentType: 'application/json', body: JSON.stringify({ error: { code: 'SESSION_NOT_FOUND', message: 'Unknown fixture session.' } }) });
      }
      if (path === '/api/goldstandard/session/' + histId) {
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...common, session_id: histId, pairs_total: 1, pairs_completed: 1, stale: true, stale_reason: 'Synthetic rechunk with pairs', stale_at: '2026-09-28T00:02:00+00:00', pairs: [{ pair_id: 'hist', question: 'Owned historical question', answer: 'Inert', contexts: ['Inert'], ground_truth: 'Inert', source_file: 'inert.txt', chunk_index: 0, status: 'approved' }] }) });
      }
      if (path === '/api/goldstandard/save' && JSON.parse(request.postData() || '{}').session_id === histId) {
        histBodies.push(JSON.parse(request.postData()));
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ filename: 'owned_hist.json', pairs_saved: 1, pairs_excluded: 0, download_url: '/api/goldstandard/download/owned_hist.json', historical: true, session_validity: { stale: true } }) });
      }
      if (path === '/api/goldstandard/download/owned_hist.json') {
        return request.respond({ status: 200, contentType: 'application/json', body: '[]' });
      }
      if (path === '/api/goldstandard/save') {
        exports++; await sleep(500);
        return request.respond({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: { message: 'Synthetic export failure.' } }) });
      }
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/goldstandard', { waitUntil: 'networkidle2' });
      async function load(id) {
        await s.page.evaluate(value => { const input = document.getElementById('retained-session'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true })); }, id);
        await sleep(100); await clickByText(s.page, 'Load / refresh session'); await sleep(200);
      }
      await load(emptyId);
      const empty = await bodyText(s.page);
      r.check('empty historical session keeps stale/orphaned reasons and timestamps visible', /Historical evaluation data/.test(empty) && /Synthetic empty history/.test(empty) && /Synthetic deleted fixture/.test(empty) && /2026-09-28T00:00/.test(empty));
      r.check('empty historical session has no pair-dependent export button', await s.page.evaluate(() => ![...document.querySelectorAll('button')].some(b => /Export.*Approved/.test(b.textContent))));
      await load(currentId);
      r.check('legacy current fixture remains exportable without warning', await s.page.evaluate(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Export Approved'); return b && !b.disabled && !document.querySelector('[role=alert]'); }));
      await s.page.evaluate(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Export Approved'); b.click(); b.click(); });
      await sleep(100);
      r.check('an export in flight indicates progress and is disabled', await s.page.evaluate(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Exporting…'); return b && b.disabled; }));
      await sleep(700);
      r.check('duplicate clicks issue one export and failure clears pending state', exports===1 && await s.page.evaluate(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Export Approved'); return b && !b.disabled && document.body.innerText.includes('Synthetic export failure.'); }));
      await load(missingId);
      r.check('a failed retained lookup clears prior pairs and export controls', /Unknown fixture session/.test(await bodyText(s.page)) && await s.page.evaluate(() => ![...document.querySelectorAll('button')].some(b=>/Export.*Approved/.test(b.textContent)) && !document.body.innerText.includes('Owned synthetic question')));
      await load(histId);
      const histButton = () => s.page.evaluate(() => { const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==='Export Historical Approved'); return b ? { disabled: b.disabled } : null; });
      const histBox = () => s.page.evaluate(() => { const c=[...document.querySelectorAll('label')].find(l=>/export historical pairs/.test(l.textContent)); const i=c && c.querySelector('input[type=checkbox]'); return i ? i.checked : null; });
      const hist = await bodyText(s.page);
      const lockedButton = await histButton();
      r.check('a stale session with pairs shows the warning and a locked historical export', /Historical evaluation data/.test(hist) && /Synthetic rechunk with pairs/.test(hist) && lockedButton && lockedButton.disabled && (await histBox()) === false, JSON.stringify(lockedButton));
      await s.page.evaluate(() => { const c=[...document.querySelectorAll('label')].find(l=>/export historical pairs/.test(l.textContent)); c.querySelector('input[type=checkbox]').click(); });
      await sleep(100);
      r.check('ticking the historical choice enables export', (await histButton())?.disabled === false);
      await load(histId);
      r.check('refreshing the session resets the historical choice', (await histBox()) === false && (await histButton())?.disabled === true);
      await s.page.evaluate(() => { const c=[...document.querySelectorAll('label')].find(l=>/export historical pairs/.test(l.textContent)); c.querySelector('input[type=checkbox]').click(); });
      await sleep(100);
      await clickByText(s.page, 'Export Historical Approved'); await sleep(400);
      r.check('historical export sends allow_historical:true and labels the result', histBodies.length === 1 && histBodies[0].allow_historical === true && /Historical data; not a current collection baseline/.test(await bodyText(s.page)), JSON.stringify(histBodies));
      r.check('historical UI fixtures cause no React page errors', !s.errors.some(error=>error.startsWith('pageerror:')));
    } finally { await s.ctx.close(); } // All synthetic responses/context owned here.
  }

  // ── role gating ────────────────────────────────────────────────────────────
  r.section('§10.4 role gating');
  {
    const s = await session(browser, BASE, 'end_user');
    await s.page.goto(BASE + '/qa', { waitUntil: 'networkidle2' }); await sleep(1500);
    const links = await s.page.evaluate(() => [...document.querySelectorAll('nav a')].map(a => a.textContent.trim()));
    r.check('End User sees only Q&A in the nav', links.length === 1 && /Q&A/.test(links[0]), JSON.stringify(links));
    await s.page.goto(BASE + '/collections', { waitUntil: 'networkidle2' }); await sleep(1200);
    const landed = await s.page.evaluate(() => location.pathname);
    r.check('End User cannot reach a gated route directly', landed === '/qa', `landed on ${landed}`);
    await s.ctx.close();
  }
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/qa', { waitUntil: 'networkidle2' }); await sleep(1500);
    const links = await s.page.evaluate(() => [...document.querySelectorAll('nav a')].map(a => a.textContent.trim()));
    for (const want of ['Q&A', 'Import', 'Chunking', 'Retrieval', 'Gold Standard', 'Transfer', 'Collections', 'Health']) {
      r.check(`Engineer nav includes ${want}`, links.includes(want), JSON.stringify(links));
    }
    await s.ctx.close();
  }

  // ── chunking explainer ─────────────────────────────────────────────────────
  r.section('§10.4 chunking explainer');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/chunking', { waitUntil: 'networkidle2' }); await sleep(2200);
    let reloads = 0; s.page.on('framenavigated', () => reloads++);
    const options = await s.page.evaluate(K => {
      const sel = [...document.querySelectorAll('select')]
        .find(x => { const v = [...x.options].map(o => o.value); return K.every(k => v.includes(k)); });
      return sel ? [...sel.options].map(o => o.value) : [];
    }, STRATEGIES);
    r.check('the strategy selector offers every strategy', options.length === STRATEGIES.length, JSON.stringify(options));
    const seen = [];
    for (const v of options) {
      await s.page.evaluate(val => {
        const sel = [...document.querySelectorAll('select')].find(x => [...x.options].some(o => o.value === val));
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(sel, val);
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }, v);
      await sleep(500);
      seen.push(await bodyText(s.page));
    }
    r.check('each strategy renders a distinct explanation',
            options.length > 1 && new Set(seen).size === options.length, `${new Set(seen).size} distinct`);
    r.check('no page reload occurs', reloads === 0, `${reloads} navigations`);
    await s.ctx.close();
  }

  // ── owned retrieval HTTP fixtures; no backend writes/model calls ───────────
  r.section('effective retrieval UI boundaries');
  {
    const s = await session(browser, BASE, 'engineer');
    const name = 'OwnedRetrievalBrowserFixture';
    const row = { name, object_count: 10, index_type: 'hnsw', distance_metric: 'cosine', hnsw_config: { ef: 72, efConstruction: 160, maxConnections: 32 } };
    let lists = 0, failRefresh = false;
    const saves = [];
    await s.page.setRequestInterception(true);
    s.page.on('request', async request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/collections') {
        lists++;
        return request.respond({ status: lists === 1 || failRefresh ? 500 : 200, contentType: 'application/json', body: JSON.stringify(lists === 1 || failRefresh ? { error: { message: 'Synthetic index read failure.' } } : { collections: [row] }) });
      }
      if (path === '/api/retrieval/config/' + name) {
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ collection: name, retrieval_mode: 'flat', top_k: 5, alpha: 0.25, ef: 96, response_format: 'engineer', is_default: false }) });
      }
      if (path === '/api/retrieval/config' && request.method() === 'POST') {
        const body = JSON.parse(request.postData()); saves.push(body);
        return request.respond({ status: 201, contentType: 'application/json', body: JSON.stringify({ ...body, is_default: false }) });
      }
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/retrieval', { waitUntil: 'networkidle2' }); await sleep(300);
      r.check('initial metadata failure displays its read warning', /Could not read the current physical index/.test(await bodyText(s.page)));
      r.check('initial metadata failure settles the pending read instead of leaving it stuck', !/Reading index details…/.test(await bodyText(s.page)) && /Index details are unavailable for this collection\./.test(await bodyText(s.page)));
      await clickByText(s.page, 'Refresh index details'); await sleep(500);
      const refreshed = await bodyText(s.page);
      r.check('successful refresh reports backend settings and clears initial warning', /ef: 72/.test(refreshed) && /efConstruction: 160/.test(refreshed) && /maxConnections: 32/.test(refreshed) && !/Could not read/.test(refreshed));
      r.check('three query methods replace inactive build controls and show legacy ef warning', await s.page.evaluate(() => document.querySelectorAll('input[name=mode]').length === 3 && document.querySelectorAll('input[type=range]').length === 1 && document.querySelector('input[value=hnsw]').checked && document.body.innerText.includes('legacy saved ef override (96) is inactive')));
      for (const limit of [1, 50]) {
        await s.page.evaluate(value => { const input = document.querySelector('input[type=range]'); Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, String(value)); input.dispatchEvent(new Event('input', { bubbles: true })); }, limit);
        await sleep(100); await clickByText(s.page, 'Save for this collection'); await sleep(300);
        r.check('Top-K ' + limit + ' is selected and sent when saving', saves.at(-1)?.top_k === limit && (await bodyText(s.page)).includes('Top-K Results: ' + limit));
      }
      r.check('saving normalizes legacy vector alias and clears inactive ef without changing observed index', saves.length === 2 && saves.every(save => save.retrieval_mode === 'hnsw' && save.ef === null) && /ef: 72/.test(await bodyText(s.page)));
      failRefresh = true;
      await clickByText(s.page, 'Refresh index details'); await sleep(300);
      r.check('failed refresh keeps prior physical details with an explicit warning', /displayed details are from the prior read/.test(await bodyText(s.page)) && /ef: 72/.test(await bodyText(s.page)));
      r.check('retrieval fixture causes no React page errors', !s.errors.some(error => error.startsWith('pageerror:')));
    } finally { await s.ctx.close(); }
  }
  for (const lateFailure of [false, true]) {
    const s = await session(browser, BASE, 'engineer');
    let initial, count = 0;
    await s.page.setRequestInterception(true);
    s.page.on('request', async request => {
      if (new URL(request.url()).pathname === '/api/collections') {
        count++;
        if (count === 1) { initial = request; return; }
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ collections: [{ name: 'OwnedLatestIndexFixture', object_count: 0, index_type: 'hnsw', distance_metric: 'cosine', hnsw_config: { ef: 191, efConstruction: 170, maxConnections: 40 } }] }) });
      }
      if (new URL(request.url()).pathname.startsWith('/api/retrieval/config/')) return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ collection: 'OwnedLatestIndexFixture', retrieval_mode: 'hnsw', top_k: 5, alpha: 0.75, ef: null, response_format: 'engineer', is_default: true }) });
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/retrieval', { waitUntil: 'domcontentloaded' }); await sleep(300);
      if (!initial) throw new Error('Initial metadata request was not observed');
      if (!lateFailure) {
        const pending = await bodyText(s.page);
        r.check('pending initial read says it is reading, not that details are unavailable', pending.includes('Reading index details…') && !/Index details are unavailable/.test(pending));
      }
      await clickByText(s.page, 'Refresh index details'); await sleep(300);
      const freshVisible = /ef: 191/.test(await bodyText(s.page));
      await initial.respond({ status: lateFailure ? 500 : 200, contentType: 'application/json', body: JSON.stringify(lateFailure ? { error: { message: 'Synthetic obsolete failure.' } } : { collections: [{ name: 'OwnedLatestIndexFixture', object_count: 0, index_type: 'flat', distance_metric: 'dot', hnsw_config: null }] }) });
      await sleep(300);
      const after = await bodyText(s.page);
      r.check('late initial ' + (lateFailure ? 'failure' : 'success') + ' cannot replace refreshed index state', freshVisible && /ef: 191/.test(after) && !/Could not read|Synthetic obsolete failure/.test(after));
    } finally { await s.ctx.close(); }
  }

  // ── Q&A page shares the accurate query-method label ─────────────────────
  r.section('Q&A accurate method label');
  {
    const s = await session(browser, BASE, 'engineer');
    const name = 'OwnedQaLabelFixture';
    await s.page.setRequestInterception(true);
    s.page.on('request', async request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/collections') {
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ collections: [{ name, object_count: 4, index_type: 'flat', distance_metric: 'cosine', hnsw_config: null }] }) });
      }
      if (path === '/api/retrieval/config/' + name) {
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ collection: name, retrieval_mode: 'flat', top_k: 5, alpha: 0.75, ef: null, response_format: 'engineer', is_default: false }) });
      }
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/qa', { waitUntil: 'networkidle2' }); await sleep(700);
      const labels = await s.page.evaluate(() => [...document.querySelectorAll('select[disabled] option')].map(o => o.textContent));
      r.check('Q&A shows the same "Vector — existing index" label for a saved flat alias', labels.includes('Vector — existing index') && !labels.includes('flat'), JSON.stringify(labels));
    } finally { await s.ctx.close(); }
  }

  // ── delete confirmation ────────────────────────────────────────────────────
  r.section('§10.4 delete confirmation');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/collections', { waitUntil: 'networkidle2' }); await sleep(2200);
    const opened = await s.page.evaluate(() => {
      const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Delete');
      if (!b) return false; b.click(); return true;
    });
    if (!opened) {
      r.skip('delete confirmation', 'no collection present to delete');
    } else {
      await sleep(700);
      const state = await s.page.evaluate(() => {
        const inputs = [...document.querySelectorAll('input')].filter(i => !i.type || i.type === 'text');
        // the modal's confirm button is the solid red one; the row link is not
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.trim() === 'Delete' && b.className.includes('bg-red-600'));
        return { inputs: inputs.length, disabled: btn ? btn.disabled : null };
      });
      r.check('the modal asks for the name to be typed', state.inputs > 0, JSON.stringify(state));
      r.check('confirm is disabled until it matches', state.disabled === true, JSON.stringify(state));
      const before = s.api.filter(x => x.method === 'DELETE').length;
      await s.page.evaluate(() => {
        const el = [...document.querySelectorAll('input')].find(i => !i.type || i.type === 'text');
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, 'not-the-name');
        el.dispatchEvent(new Event('input', { bubbles: true }));
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.trim() === 'Delete' && b.className.includes('bg-red-600'));
        if (btn && !btn.disabled) btn.click();
      });
      await sleep(900);
      r.check('a wrong name sends no DELETE',
              s.api.filter(x => x.method === 'DELETE').length === before);

      // A refused delete (409 while a job runs, #40) is shown in the dialog. The
      // page-level error line sits behind the overlay, where it read as nothing.
      // The DELETE is answered here, so no collection is touched.
      const busyMessage = "An ingest job is running for this collection ('Owned', job 0wned40). Delete the collection after the job finishes.";
      await s.page.setRequestInterception(true);
      s.page.on('request', request => {
        if (request.method() === 'DELETE' && new URL(request.url()).pathname.startsWith('/api/collections/')) {
          return request.respond({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: { code: 'COLLECTION_BUSY', message: busyMessage, detail: { job_kind: 'ingest', job_id: '0wned40' } } }) });
        }
        return request.continue();
      });
      const target = await s.page.evaluate(() => {
        const el = [...document.querySelectorAll('input')].find(i => !i.type || i.type === 'text');
        const name = el.placeholder;
        Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, name);
        el.dispatchEvent(new Event('input', { bubbles: true }));
        return name;
      });
      await sleep(200);
      const deletes = s.api.filter(x => x.method === 'DELETE').length;
      await s.page.evaluate(() => {
        const btn = [...document.querySelectorAll('button')]
          .find(b => b.textContent.trim() === 'Delete' && b.className.includes('bg-red-600'));
        if (btn && !btn.disabled) btn.click();
      });
      await sleep(900);
      const refused = await s.page.evaluate(() => {
        const dialog = [...document.querySelectorAll('h2')].find(h => h.textContent.trim() === 'Delete Collection');
        const box = dialog && dialog.parentElement;
        const alert = box && box.querySelector('[role=alert]');
        return { open: !!dialog, alert: alert ? alert.textContent : null };
      });
      r.check('a refused delete sends one DELETE', s.api.filter(x => x.method === 'DELETE').length === deletes + 1);
      r.check('a refused delete keeps the dialog open and shows the message in it',
              refused.open && refused.alert === busyMessage, JSON.stringify(refused));
      const listed = await s.page.evaluate(t => [...document.querySelectorAll('tbody td')].some(td => td.textContent.trim() === t), target);
      r.check('the refused collection is still listed', listed, target);
      await clickByText(s.page, 'Cancel'); await sleep(200);
      await s.page.evaluate(() => {
        const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === 'Delete');
        if (b) b.click();
      });
      await sleep(300);
      r.check('cancelling clears the message for the next attempt',
              await s.page.evaluate(() => !document.querySelector('[role=alert]')));
    }
    await s.ctx.close();
  }

  // ── upload size limit ──────────────────────────────────────────────────────
  // The Import page refuses a selection over the proxy's limit before sending.
  // The file is sparse: it reports 513 MB but occupies no disk, and the check
  // must stop it before the browser ever reads it. See issue #21.
  r.section('upload size limit');
  {
    const fs = require('fs');
    const big = '/tmp/vfy-oversize-upload.txt';
    fs.closeSync(fs.openSync(big, 'w'));
    fs.truncateSync(big, 513 * 1024 * 1024);
    const s = await session(browser, BASE, 'developer');
    await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
    const hint = await bodyText(s.page);
    r.check('the drop zone states the upload limit', hint.includes('up to 512 MB per upload'));
    const input = await s.page.$('#file-input');
    await input.uploadFile(big);
    await sleep(500);
    const posts = () => s.api.filter(x => x.method === 'POST' && x.url.includes('/ingest/upload')).length;
    const before = posts();
    const clicked = await clickByText(s.page, 'Start Ingest');
    await sleep(900);
    const text = await bodyText(s.page);
    r.check('an oversize selection is refused with the limit named',
            clicked && text.includes('one upload can be at most 512 MB'),
            clicked ? text.slice(0, 160) : 'Start Ingest button not found');
    r.check('an oversize selection sends no upload', posts() === before);
    r.check('no console errors on the import page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    fs.unlinkSync(big);
    await s.ctx.close();
  }

  // ── import file types (#291) ───────────────────────────────────────────────
  // The picker offers JSON Lines, plain and gzipped, and the hint names it.
  r.section('import file types');
  {
    const s = await session(browser, BASE, 'developer');
    try {
      await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
      const accept = await s.page.evaluate(() => {
        const input = document.querySelector('#file-input');
        return input ? input.getAttribute('accept') : null;
      });
      r.check('the file picker accepts .jsonl and .jsonl.gz',
              accept === '.pdf,.docx,.txt,.md,.csv,.json,.jsonl,.jsonl.gz,.zip', String(accept));
      const hint = await bodyText(s.page);
      r.check('the drop zone hint names JSONL among the types',
              hint.includes('PDF, DOCX, TXT, MD, CSV, JSON, JSONL, ZIP · up to 512 MB per upload'),
              hint.slice(0, 200));
    } finally {
      await s.ctx.close();
    }
  }

  // ── import errors (#290) ───────────────────────────────────────────────────
  // A truncated ZIP once ended in a plain-text 500 that the page could only
  // show as "HTTP 500", and a failing status poll was ignored.
  r.section('import error messages');
  {
    const fs = require('fs');
    const truncated = '/tmp/vfy-truncated.zip';
    // A local file header and nothing after it: no central directory.
    fs.writeFileSync(truncated, Buffer.concat([Buffer.from('PK\x03\x04', 'latin1'), Buffer.alloc(64, 0x61)]));
    const s = await session(browser, BASE, 'developer');
    try {
      await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
      const input = await s.page.$('#file-input');
      await input.uploadFile(truncated);
      await sleep(300);
      const clicked = await clickByText(s.page, 'Start Ingest');
      await sleep(2500);
      const text = await bodyText(s.page);
      r.check("a truncated ZIP shows the API's message, not HTTP 500",
              clicked && text.includes("is damaged and can't be read") && !text.includes('HTTP 500'),
              clicked ? text.slice(0, 200) : 'Start Ingest button not found');
    } finally {
      fs.unlinkSync(truncated);
      await s.ctx.close();
    }
  }
  {
    const fs = require('fs');
    const small = '/tmp/vfy-poll.txt';
    fs.writeFileSync(small, 'Polling fixture.\n');
    const s = await session(browser, BASE, 'developer');
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/ingest/upload' && request.method() === 'POST')
        return request.respond({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'vfypoll1', status: 'queued', files_queued: 1, collection: 'x' }) });
      if (path === '/api/ingest/job/vfypoll1')
        return request.respond({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: { code: 'X', message: 'Synthetic status failure.', detail: null } }) });
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
      const input = await s.page.$('#file-input');
      await input.uploadFile(small);
      await sleep(300);
      const clicked = await clickByText(s.page, 'Start Ingest');
      await sleep(4500);
      const text = await bodyText(s.page);
      r.check('a failing status poll is shown, not ignored',
              clicked && text.includes("Could not check the import's progress: Synthetic status failure."),
              clicked ? text.slice(0, 200) : 'Start Ingest button not found');
      r.check('import error fixtures cause no React page errors', !s.errors.some(e => e.startsWith('pageerror:')));
    } finally {
      fs.unlinkSync(small);
      await s.ctx.close();
    }
  }
  // A poll error is cleared by a new ingest and by the next successful poll
  // (#298). Job A's polls always fail; job B's first poll fails, then it
  // completes. Polls run every 3 s.
  {
    const fs = require('fs');
    const small = '/tmp/vfy-poll-clear.txt';
    fs.writeFileSync(small, 'Polling fixture.\n');
    const s = await session(browser, BASE, 'developer');
    const failure = JSON.stringify({ error: { code: 'X', message: 'Synthetic status failure.', detail: null } });
    let uploads = 0, pollsB = 0;
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/ingest/upload' && request.method() === 'POST') {
        uploads += 1;
        const job = uploads === 1 ? 'vfyclrA' : 'vfyclrB';
        return request.respond({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: job, status: 'queued', files_queued: 1, collection: 'x' }) });
      }
      if (path === '/api/ingest/job/vfyclrA')
        return request.respond({ status: 500, contentType: 'application/json', body: failure });
      if (path === '/api/ingest/job/vfyclrB') {
        pollsB += 1;
        if (pollsB === 1) return request.respond({ status: 500, contentType: 'application/json', body: failure });
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ job_id: 'vfyclrB', status: 'completed', files_total: 1, files_completed: 1, files_failed: 0, chunks_stored: 2, chunks_replaced: 0, errors: [], skipped: [] }) });
      }
      return request.continue();
    });
    const shown = text => text.includes("Could not check the import's progress: Synthetic status failure.");
    try {
      await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
      const input = await s.page.$('#file-input');
      await input.uploadFile(small);
      await sleep(300);
      const first = await clickByText(s.page, 'Start Ingest');
      await sleep(4500);                       // job A's first poll has failed
      const before = shown(await bodyText(s.page));
      const second = await clickByText(s.page, 'Start Ingest');
      await sleep(1000);                       // job B accepted, not yet polled
      const afterNew = await bodyText(s.page);
      r.check('a new ingest clears a poll error',
              first && second && before && uploads === 2 && !shown(afterNew),
              `clicked=${first},${second} shown-before=${before} uploads=${uploads} after: ${afterNew.slice(0, 160)}`);
      await sleep(2700);                       // job B's first poll has failed
      const failedAgain = shown(await bodyText(s.page));
      await sleep(3000);                       // job B's second poll has succeeded
      const afterPoll = await bodyText(s.page);
      r.check('the next successful poll clears a poll error',
              failedAgain && pollsB >= 2 && !shown(afterPoll),
              `shown-after-B's-first-poll=${failedAgain} B-polls=${pollsB} after: ${afterPoll.slice(0, 160)}`);
    } finally {
      fs.unlinkSync(small);
      await s.ctx.close();
    }
  }

  // ── re-ingest progress (#22) ───────────────────────────────────────────────
  // The progress panel reports replaced chunks, and the skipped heading no
  // longer claims every skip is an unsupported type: a later file with the
  // same name in one upload is skipped too. Job responses are stubbed.
  r.section('re-ingest progress');
  for (const [replaced, label] of [[3, 'Replaced 3 chunks from a previous version'], [1, 'Replaced 1 chunk from a previous version'], [0, null]]) {
    const fs = require('fs');
    const small = '/tmp/vfy-reingest.txt';
    fs.writeFileSync(small, 'Re-ingest fixture.\n');
    const jobId = 'vfyrein' + replaced;
    const skippedEntry = "u.zip:a/r.txt (replaced by r.txt later in this upload, same name 'a/r.txt')";
    const s = await session(browser, BASE, 'developer');
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/ingest/upload' && request.method() === 'POST')
        return request.respond({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: jobId, status: 'queued', files_queued: 1, collection: 'x' }) });
      if (path === '/api/ingest/job/' + jobId)
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ job_id: jobId, status: 'completed', files_total: 1, files_completed: 1, files_failed: 0, chunks_stored: 2, errors: [], skipped: [skippedEntry], chunks_replaced: replaced }) });
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
      const input = await s.page.$('#file-input');
      await input.uploadFile(small);
      await sleep(300);
      const clicked = await clickByText(s.page, 'Start Ingest');
      await sleep(4500);
      const text = await bodyText(s.page);
      if (label) {
        r.check(`chunks_replaced=${replaced} is shown as "${label}"`, clicked && text.includes(label),
                clicked ? text.slice(0, 240) : 'Start Ingest button not found');
      } else {
        r.check('chunks_replaced=0 shows no replacement line', clicked && text.includes('Chunks stored: 2') && !text.includes('Replaced '),
                clicked ? text.slice(0, 240) : 'Start Ingest button not found');
        r.check('the skipped heading is a plain "1 file skipped:" with the entry\'s own reason',
                text.includes('1 file skipped:') && !text.includes('not a supported type:') && text.includes(skippedEntry),
                text.slice(0, 300));
      }
      r.check(`re-ingest progress fixture (${replaced}) causes no React page errors`, !s.errors.some(e => e.startsWith('pageerror:')));
    } finally {
      fs.unlinkSync(small);
      await s.ctx.close();
    }
  }

  // ── ingest write counts and a delete in progress (#302) ──────────────────
  // The progress panel shows submitted, accepted and stored chunks only when
  // they differ; a clean job and an older record without the counts look as
  // before. An upload refused while its collection is being deleted shows the
  // API's message. Responses are stubbed.
  r.section('ingest write counts');
  const countCases = [
    ['differing counts', { chunks_submitted: 10, chunks_accepted: 10, chunks_stored: 0, status: 'failed', files_completed: 0, files_failed: 1 }, 'Submitted 10 · accepted 10 · stored 0'],
    ['equal counts', { chunks_submitted: 2, chunks_accepted: 2, chunks_stored: 2, status: 'completed', files_completed: 1, files_failed: 0 }, null],
    ['a record without the counts', { chunks_stored: 5, status: 'completed', files_completed: 1, files_failed: 0 }, null],
  ];
  for (const [label, fields, line] of countCases) {
    const fs = require('fs');
    const small = '/tmp/vfy-counts.txt';
    fs.writeFileSync(small, 'Write counts fixture.\n');
    const jobId = 'vfycnt' + countCases.findIndex(c => c[0] === label);
    const s = await session(browser, BASE, 'developer');
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/ingest/upload' && request.method() === 'POST')
        return request.respond({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: jobId, status: 'queued', files_queued: 1, collection: 'x' }) });
      if (path === '/api/ingest/job/' + jobId)
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ job_id: jobId, files_total: 1, errors: [], skipped: [], chunks_replaced: 0, ...fields }) });
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
      const input = await s.page.$('#file-input');
      await input.uploadFile(small);
      await sleep(300);
      const clicked = await clickByText(s.page, 'Start Ingest');
      await sleep(4500);
      const text = await bodyText(s.page);
      if (line) {
        r.check(`${label}: the panel shows "${line}"`, clicked && text.includes(line),
                clicked ? text.slice(0, 240) : 'Start Ingest button not found');
      } else {
        r.check(`${label}: the panel shows no submitted/accepted line`,
                clicked && text.includes(`Chunks stored: ${fields.chunks_stored}`) && !text.includes('Submitted '),
                clicked ? text.slice(0, 240) : 'Start Ingest button not found');
      }
      r.check(`write counts fixture (${label}) causes no React page errors`, !s.errors.some(e => e.startsWith('pageerror:')));
    } finally {
      fs.unlinkSync(small);
      await s.ctx.close();
    }
  }
  {
    const fs = require('fs');
    const small = '/tmp/vfy-deleting.txt';
    fs.writeFileSync(small, 'Deleting fixture.\n');
    const message = "Collection 'VfyGone' is being deleted. Start the job again once the delete finishes; the collection must be created again first.";
    const s = await session(browser, BASE, 'developer');
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/ingest/upload' && request.method() === 'POST')
        return request.respond({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: { code: 'COLLECTION_DELETING', message, detail: { collection: 'VfyGone' } } }) });
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/import', { waitUntil: 'networkidle2' }); await sleep(1500);
      const input = await s.page.$('#file-input');
      await input.uploadFile(small);
      await sleep(300);
      const clicked = await clickByText(s.page, 'Start Ingest');
      await sleep(1500);
      const text = await bodyText(s.page);
      r.check('an upload refused with 409 COLLECTION_DELETING shows its message', clicked && text.includes(message),
              clicked ? text.slice(0, 300) : 'Start Ingest button not found');
    } finally {
      fs.unlinkSync(small);
      await s.ctx.close();
    }
  }

  // ── health dashboard ───────────────────────────────────────────────────────
  r.section('§10.4 health dashboard');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/health', { waitUntil: 'networkidle2' }); await sleep(2500);
    const body = await bodyText(s.page);
    const latencies = body.match(/\d+\s*ms/g) || [];
    r.check('per-service latency is shown', latencies.length >= 3, latencies.slice(0, 5).join(' '));
    // Services are labelled by role and model, not by the word "Ollama".
    for (const want of ['Weaviate', 'LLM', 'Embed']) {
      r.check(`the dashboard names ${want}`, new RegExp(want, 'i').test(body));
    }
    if (process.env.RAG_SKIP_SLOW === '1') {
      r.skip('30s auto-refresh', 'needs a 70s observation window');
    } else {
      const t0 = Date.now(); s.api.length = 0;
      await sleep(70000);
      const hits = s.api.filter(x => x.url.includes('/health')).map(x => Math.round((x.at - t0) / 1000));
      const gaps = hits.slice(1).map((v, i) => v - hits[i]);
      r.check('the dashboard refreshes on its own', hits.length >= 2, `polled at t+${hits.join('s, t+')}s`);
      r.check('the interval is about 30s', gaps.length > 0 && gaps.every(g => g >= 25 && g <= 35), `gaps: ${gaps.join(', ')}s`);
    }
    r.check('no console errors on the health page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    await s.ctx.close();
  }

  // ── degraded and unreachable health (#375) ─────────────────────────────────
  // /health answers 503 with its per-service body when a dependency is down. The
  // page must show that body, red for the failing service, keep loading the
  // latency metrics, and say so when health can't be read at all. Each load is
  // driven by hand through the captured 30s refresh, with the answers stubbed.
  r.section('degraded and unreachable health (#375)');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.evaluateOnNewDocument(() => {
      const original = window.setInterval;
      window.setInterval = (fn, delay, ...args) => {
        if (delay === 30000) { window.__ownedHealthRefresh = fn; return 45002; }
        return original(fn, delay, ...args);
      };
    });
    const service = (status, latency_ms, model) => model ? { status, latency_ms, model } : { status, latency_ms };
    const healthBody = (status, embed) => ({
      status,
      services: {
        weaviate: service('ok', 11),
        ollama: { llm: service('ok', 22, 'phi3.5'), embed: service(embed, 33, 'nomic-embed-text') },
      },
      resources: { memory: { status: 'ok', allocated_gb: 11.67, recommended_minimum_gb: 12.0 } },
    });
    const proxyPage = code => `<html><head><title>${code}</title></head><body><center><h1>${code}</h1></center><hr><center>nginx</center></body></html>`;
    const metricsBody = n => ({
      total_records: n,
      retrieval_latency: { p50: 10, p95: 20, p99: 30 },
      llm_latency: { p50: 100, p95: 200, p99: 300 },
      total_latency: { p50: 110, p95: 220, p99: 330 },
      history: Array.from({ length: n }, (_, i) => ({ timestamp: new Date(Date.UTC(2026, 9, 1, 0, 0, i)).toISOString(), collection: 'OwnedFixture', retrieval_mode: 'hybrid', retrieval_ms: 10, llm_ms: 100, total_ms: 110 })),
    });
    let mode = 'degraded', loads = 0;
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      if (path === '/api/health') {
        if (mode === 'refused') return request.abort('connectionrefused');
        if (mode === 'proxy502') return request.respond({ status: 502, contentType: 'text/html', body: proxyPage('502 Bad Gateway') });
        if (mode === 'html503') return request.respond({ status: 503, contentType: 'text/html', body: proxyPage('503 Service Temporarily Unavailable') });
        const degraded = mode === 'degraded';
        return request.respond({ status: degraded ? 503 : 200, contentType: 'application/json', body: JSON.stringify(healthBody(degraded ? 'degraded' : 'ok', degraded ? 'error' : 'ok')) });
      }
      if (path === '/api/metrics/latency') {
        loads++;
        return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(metricsBody(loads)) });
      }
      return request.continue();
    });
    // Each service card's label and dot colour; the latency tiles have no dot.
    const cards = () => s.page.evaluate(() => [...document.querySelectorAll('span.rounded-full.w-2')].map(dot => ({
      label: (dot.parentElement.textContent || '').trim(),
      red: dot.classList.contains('bg-red-500'), green: dot.classList.contains('bg-green-500'),
    })));
    const colours = list => list.map(c => `${c.label}=${c.red ? 'red' : c.green ? 'green' : '?'}`).join(', ');
    const healthAlert = text => (text.match(/Service health could not be read:.{0,80}/) || [''])[0];
    const refresh = async next => { mode = next; await s.page.evaluate(() => window.__ownedHealthRefresh()); await sleep(700); };
    const metricsShown = (text, n) => text.includes(`Latency Trends (${n} queries in ring buffer)`);
    try {
      await s.page.goto(BASE + '/health', { waitUntil: 'networkidle2' }); await sleep(700);
      let text = await bodyText(s.page), list = await cards();
      const by = label => list.find(c => c.label.startsWith(label)) || {};
      r.check('a degraded 503 shows the failing service red', list.length === 3 && by('Embed').red, colours(list));
      r.check('a degraded 503 shows the services that are ok green', by('Weaviate').green && by('LLM').green, colours(list));
      r.check('a degraded 503 shows each service latency', ['11ms', '22ms', '33ms'].every(ms => text.includes(ms)), text.slice(0, 200));
      r.check('a degraded 503 is not reported as unreadable', !healthAlert(text), healthAlert(text));
      r.check('latency metrics load while health answers 503', metricsShown(text, 1), text.slice(0, 300));

      await refresh('ok'); list = await cards(); text = await bodyText(s.page);
      r.check('a 200 shows every service green', list.length === 3 && list.every(c => c.green), colours(list));

      await refresh('proxy502'); list = await cards(); text = await bodyText(s.page);
      r.check('an API down behind the proxy (502) shows a message',
              /Service health could not be read: The API is not responding/.test(text), text.slice(0, 300));
      r.check('a failed health load removes the earlier green cards', list.length === 0, colours(list));
      r.check('latency metrics load while the health call fails', metricsShown(text, 3), text.slice(0, 300));

      await refresh('ok'); list = await cards(); text = await bodyText(s.page);
      r.check('the next good health load clears the message and shows the cards',
              !healthAlert(text) && list.length === 3 && list.every(c => c.green), healthAlert(text) || colours(list));

      await refresh('refused'); list = await cards(); text = await bodyText(s.page);
      r.check('an unreachable API shows a message',
              /Service health could not be read: The API could not be reached/.test(text), text.slice(0, 300));
      r.check('an unreachable API shows no service cards', list.length === 0, colours(list));
      r.check('latency metrics load while the API is unreachable for health', metricsShown(text, 5), text.slice(0, 300));

      await refresh('html503'); list = await cards(); text = await bodyText(s.page);
      r.check('a 503 without a health body shows a message, not cards',
              /Service health could not be read: HTTP 503/.test(text) && list.length === 0, text.slice(0, 300));
      r.check('degraded and unreachable health cause no React page errors', !s.errors.some(e => e.startsWith('pageerror:')),
              s.errors.filter(e => e.startsWith('pageerror:')).slice(0, 2).join(' | '));
    } finally { await s.ctx.close(); }
  }

  // ── owned session-recovery diagnostic HTTP fixtures ───────────────────────
  r.section('session recovery diagnostics');
  for (const failure of [false, true]) {
    const s = await session(browser, BASE, 'engineer');
    await s.page.setRequestInterception(true);
    s.page.on('request', async request => {
      if (new URL(request.url()).pathname === '/api/goldstandard/diagnostics') {
        return request.respond({ status: failure ? 503 : 200, contentType: 'application/json', body: JSON.stringify(failure ? { error: { message: 'Synthetic diagnostic read failure' } } : { issues: [{ filename: 'gs_ownedfixture.json', code: 'SESSION_READ_FAILED', message: 'Owned unreadable snapshot preserved.' }] }) });
      }
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/health', { waitUntil: 'networkidle2' }); await sleep(500);
      const text = await bodyText(s.page);
      if (failure) {
        r.check('diagnostic refresh failure is visible', /Session recovery diagnostics could not be refreshed/.test(text));
      } else {
        r.check('retained-session recovery warning is visible on Health', /Evaluation session recovery needs attention/.test(text));
        r.check('recovery warning exposes filename, code and preservation message', /gs_ownedfixture.json/.test(text) && /SESSION_READ_FAILED/.test(text) && /Owned unreadable snapshot preserved/.test(text));
      }
      r.check('diagnostic ' + (failure ? 'failure' : 'warning') + ' does not cause React page errors', !s.errors.some(error => error.startsWith('pageerror:')));
    } finally { await s.ctx.close(); }
  }

  // Controlled refresh timing exercises visible pending, failure and ordering.
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.evaluateOnNewDocument(() => {
      const original = window.setInterval;
      window.setInterval = (fn, delay, ...args) => {
        if (delay === 30000) { window.__ownedHealthRefresh = fn; return 45001; }
        return original(fn, delay, ...args);
      };
    });
    const pending = [];
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      if (new URL(request.url()).pathname === '/api/goldstandard/diagnostics') pending.push(request);
      else request.continue();
    });
    const next = async () => {
      for (let i = 0; i < 100 && !pending.length; i++) await sleep(50);
      if (!pending.length) throw new Error('Owned diagnostic refresh did not arrive');
      return pending.shift();
    };
    const respond = (request, filename, status = 200) => request.respond({ status, contentType: 'application/json', body: JSON.stringify(status === 200 ? { issues: [{ filename, code: 'SESSION_READ_FAILED', message: 'Owned retained result.' }] } : { error: { message: 'Owned refresh failure' } }) });
    const refresh = () => s.page.evaluate(() => window.__ownedHealthRefresh());
    try {
      await s.page.goto(BASE + '/health', { waitUntil: 'domcontentloaded' });
      const initial = await next(); await sleep(150);
      r.check('initial diagnostic pending state is visible', /Refreshing session recovery diagnostics/.test(await bodyText(s.page)));
      await respond(initial, 'gs_previousfixture.json'); await sleep(250);
      await refresh(); const failed = await next(); await sleep(100);
      let text = await bodyText(s.page);
      r.check('pending refresh labels retained diagnostics as previous results', /Previous evaluation session recovery results/.test(text) && /gs_previousfixture.json/.test(text));
      await respond(failed, '', 503); await sleep(250); text = await bodyText(s.page);
      r.check('failed refresh removes old current-issue claims and clears pending state', /could not be refreshed/.test(text) && !/gs_previousfixture.json|Refreshing session recovery diagnostics/.test(text));
      await refresh(); const recovered = await next(); await respond(recovered, 'gs_currentfixture.json'); await sleep(250); text = await bodyText(s.page);
      r.check('successful refresh clears the error and pending indicators', /gs_currentfixture.json/.test(text) && !/could not be refreshed|Refreshing session recovery diagnostics/.test(text));
      await refresh(); const older = await next(); await refresh(); const newer = await next();
      await respond(newer, 'gs_latestfixture.json'); await sleep(200);
      await respond(older, '', 503); await sleep(250); text = await bodyText(s.page);
      r.check('late failed response cannot overwrite newer diagnostic success', /gs_latestfixture.json/.test(text) && !/could not be refreshed/.test(text));
      await refresh(); const olderSuccess = await next(); await refresh(); const newerSuccess = await next();
      await respond(newerSuccess, 'gs_finalfixture.json'); await sleep(150);
      await respond(olderSuccess, 'gs_stalefixture.json'); await sleep(250); text = await bodyText(s.page);
      r.check('late successful response cannot replace newer diagnostic results', /gs_finalfixture.json/.test(text) && !/gs_stalefixture.json/.test(text));
      r.check('refresh error and timing fixtures do not cause React page errors', !s.errors.some(error => error.startsWith('pageerror:')));
    } finally { await s.ctx.close(); }
  }

  // Imported identities are exposed through the existing visible job notes.
  r.section('imported session lookup IDs');
  {
    const s = await session(browser, BASE, 'engineer');
    const filename = 'ragpkg-owned-session.tar.gz';
    const sourceId = 'gs_460abcde', localId = 'gs_460abcdf';
    const submissions = [];
    await s.page.setRequestInterception(true);
    s.page.on('request', request => {
      const path = new URL(request.url()).pathname;
      let body;
      if (path === '/api/packages') body = { packages: [{ filename, size_bytes: 100, collection: 'OwnedOriginal', chunk_count: 1, fidelity: 'chunks-only', created_at: '2026-09-28T00:00:00Z', readable: true }] };
      else if (path === '/api/import' && request.method() === 'POST') {
        submissions.push(JSON.parse(request.postData()));
        return request.respond({ status: 202, contentType: 'application/json', body: JSON.stringify({ job_id: 'owned-identity-job', status: 'queued', filename }) });
      } else if (path === '/api/import/job/owned-identity-job') body = { job_id: 'owned-identity-job', status: 'completed', filename, on_conflict: 'rename', collection: 'OwnedImported', original_collection: 'OwnedOriginal', chunks_written: 1, fidelity: 'chunks-only', renamed: true, notes: ["evaluation session '" + sourceId + "' restored as local '" + localId + "' for 'OwnedImported'"], restored_sessions: [{ source_session_id: sourceId, session_id: localId, collection: 'OwnedImported' }], error: null, error_code: null, error_detail: null };
      if (body) return request.respond({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
      return request.continue();
    });
    try {
      await s.page.goto(BASE + '/transfer', { waitUntil: 'networkidle2' }); await sleep(250);
      await s.page.evaluate(value => {
        const selector = [...document.querySelectorAll('select')].find(el => [...el.options].some(option => option.value === value));
        Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(selector, value);
        selector.dispatchEvent(new Event('change', { bubbles: true }));
        document.querySelector('input[name="conflict"][value="rename"]').click();
      }, filename);
      await s.page.evaluate(() => [...document.querySelectorAll('button')].find(button => button.textContent.trim() === 'Import').click()); await sleep(600);
      const text = await bodyText(s.page);
      r.check('rename import submits selected package and explicit policy', submissions.length === 1 && submissions[0].filename === filename && submissions[0].on_conflict === 'rename');
      r.check('completed import displays original and allocated session IDs for lookup', text.includes(sourceId) && text.includes(localId) && text.includes('OwnedImported'));
      r.check('import identity notes do not cause React page errors', !s.errors.some(error => error.startsWith('pageerror:')));
    } finally { await s.ctx.close(); }
  }

  // ── transfer help page ─────────────────────────────────────────────────────
  r.section('transfer help page');
  {
    const s = await session(browser, BASE, 'engineer');
    await s.page.goto(BASE + '/help/transfer', { waitUntil: 'networkidle2' }); await sleep(2500);
    const info = await s.page.evaluate(() => {
      const h1 = document.querySelector('h1');
      const p = document.querySelector('article p');
      return {
        chars: (document.querySelector('#root')?.innerText || '').length,
        headings: [...document.querySelectorAll('h2')].map(e => e.textContent.trim()),
        tables: document.querySelectorAll('table').length,
        h1Size: h1 ? parseFloat(getComputedStyle(h1).fontSize) : 0,
        pSize: p ? parseFloat(getComputedStyle(p).fontSize) : 0,
      };
    });
    r.check('the help page renders substantive content', info.chars > 3000, `${info.chars} chars`);
    r.check('markdown tables render', info.tables >= 3, `${info.tables} tables`);
    r.check('headings are styled (typography plugin present)', info.h1Size > info.pSize,
            `h1=${info.h1Size}px p=${info.pSize}px`);
    for (const want of ['Where packages live', 'Naming', 'What a package contains',
                        'Fidelity', 'embedding model rule', 'name collision', 'Tuning after import']) {
      r.check(`§10 topic covered: ${want}`,
              info.headings.some(h => h.toLowerCase().includes(want.toLowerCase())),
              info.headings.join(' | ').slice(0, 80));
    }
    r.check('no console errors on the help page', s.errors.length === 0, s.errors.slice(0, 2).join(' | '));
    await s.ctx.close();
  }

  await browser.close();
  process.exit(r.summary() ? 0 : 1);
})().catch(e => { console.log('  HARNESS FAILURE: ' + e.message); process.exit(2); });
