// UI pages whose controls had no browser check (#400): Gold Standard generate and
// review, the Transfer page's export and import controls, the Help page and the
// Health latency display. Every API call is answered by the check (see stub.js).
// Non-ASCII UI text is written as \u escapes. The Transfer polling and /tune
// failure labels are checked by transfer_page.js (#382) and not repeated here.
const assert = require('node:assert/strict');
const S = require('./stub');
const { json, apiError, html, hold, callsTo, waitFor, waitText, press, fill, fillNth, buttonState, waitPath } = S;

const A = 'VfyUiA', B = 'VfyUiB';
const DOT = '\u00b7', DASH = '\u2014', ELLIPSIS = '\u2026', ARROW = '\u2192';
const COLS = [
  { name: A, object_count: 12, index_type: 'hnsw', distance_metric: 'cosine', created_at: null, hnsw_config: null },
  { name: B, object_count: 0, index_type: 'flat', distance_metric: 'dot', created_at: null, hnsw_config: null },
];
const bodyHas = (page, want) => page.evaluate(w => document.body.innerText.replace(/\s+/g, ' ').includes(w), want);
const tune = (name, fidelity = 'with-sources') => ({ collection: name, fidelity, source_document_count: 2, can_rechunk: fidelity === 'with-sources', can_reembed: true, can_reindex: true, note: '' });
const common = (extra = {}) => ({
  'GET /api/collections': json(200, { collections: COLS }),
  'GET /api/packages': json(200, { packages: [] }),
  'GET /api/tune/*': call => json(200, tune(call.path.split('/').pop())),
  'GET /api/help/transfer': json(200, { topic: 'transfer', markdown: '# Help\n\nSome text.' }),
  ...extra,
});
const waitSelected = (page, i) => waitFor(page, n => !!document.querySelectorAll('select')[n]?.value, 'the collection select to be filled', i);

// -- Gold Standard -----------------------------------------------------------
const SID = 'gs_vfyui01';
const pair = (id, status, question = 'Question ' + id) => ({ pair_id: id, question, answer: 'Answer ' + id, contexts: ['Context ' + id], ground_truth: 'Truth ' + id, source_file: id + '.txt', chunk_index: 0, status });
const session = (pairs, over = {}) => ({ session_id: SID, collection: A, status: 'completed', pairs_total: pairs.length, pairs_attempted: pairs.length, pairs_completed: pairs.length, pairs_failed: 0, pairs, ...over });
const reviewPairs = () => [pair('p1', 'pending'), pair('p2', 'approved'), pair('p3', 'edited'), pair('p4', 'rejected')];
const SESSION = '/api/goldstandard/session/' + SID;

// Loads a retained session by id, which shows the review phase without generating.
async function loadSession(page) {
  await waitSelected(page, 0);
  await fill(page, '#retained-session', SID);
  await press(page, 'Load / refresh session');
  await waitText(page, 'Phase 2');
}
const summary = page => page.evaluate(() => [...document.querySelectorAll('p')].find(p => /approved .* pending/.test(p.textContent))?.textContent.replace(/\s+/g, ' ').trim());
const pairControl = (page, question, title) => page.evaluate((q, t) => {
  const d = [...document.querySelectorAll('details')].find(x => x.querySelector('summary .font-medium')?.textContent.trim() === q);
  const b = d?.querySelector(`button[title=${t}]`);
  if (!b) return false; b.click(); return true;
}, question, title);
const detailsOpen = (page, question) => page.evaluate(q => [...document.querySelectorAll('details')].find(x => x.querySelector('summary .font-medium')?.textContent.trim() === q)?.open, question);
const statusOf = (page, question) => page.evaluate(q => [...document.querySelectorAll('details')].find(x => x.querySelector('summary .font-medium')?.textContent.trim() === q)?.querySelector('summary span.rounded')?.textContent.trim(), question);

function goldCases() {
  const gold = (routes = {}, over = {}) => ({ role: 'engineer', path: '/goldstandard', ...over, routes: common(routes) });
  // A small server: the page polls the session once after loading, so the answers
  // must follow the changes the page makes.
  const review = (routes = {}, pairs = reviewPairs()) => gold({
    ['GET ' + SESSION]: () => json(200, session(pairs)),
    ['PATCH ' + SESSION + '/pair/*']: call => {
      const i = pairs.findIndex(p => p.pair_id === call.path.split('/').pop());
      pairs[i] = { ...pairs[i], ...call.body };
      return json(200, pairs[i]);
    },
    'POST /api/goldstandard/regenerate': call => {
      const i = pairs.findIndex(p => p.pair_id === call.body.pair_id);
      pairs[i] = { ...pairs[i], question: 'Regenerated question', answer: 'New answer', status: 'pending' };
      return json(200, pairs[i]);
    },
    ...routes,
  });
  let confirmAnswer = false;
  return [
    { name: 'Gold Standard lists the collections with the first selected, and the sample size input is 1 to 100 starting at 20', open: gold(), async run(s) {
      await waitSelected(s.page, 0);
      assert.deepEqual(await s.page.evaluate(() => [...document.querySelectorAll('select option')].map(o => o.value)), [A, B]);
      assert.equal(await s.page.evaluate(() => document.querySelector('select').value), A);
      assert.deepEqual(await s.page.evaluate(() => { const i = document.querySelector('input[type=number]'); return { min: i.min, max: i.max, value: i.value }; }), { min: '1', max: '100', value: '20' });
    } },
    (() => {
      const h = hold();
      return { name: 'Generate sends the chosen collection and sample size once and is disabled until the start is answered and the session settles', open: gold({
        'POST /api/goldstandard/generate': () => h,
        ['GET ' + SESSION]: json(200, session(reviewPairs())) }, { fastPolls: true }), async run(s) {
        await waitSelected(s.page, 0);
        await fillNth(s.page, 'select', 0, B);
        await fill(s.page, 'input[type=number]', 5);
        await press(s.page, 'Generate Pairs');
        await waitFor(s.page, () => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Generate Pairs')?.disabled === true, 'Generate Pairs to be disabled');
        h.release(json(202, { session_id: SID, status: 'generating', pairs_total: 4, pairs_completed: 0 }));
        await waitFor(s.page, () => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Generate Pairs')?.disabled === false, 'Generate Pairs to be enabled');
        const posts = callsTo(s, 'POST', '/api/goldstandard/generate');
        assert.equal(posts.length, 1);
        assert.deepEqual(posts[0].body, { collection: B, sample_size: 5 });
      } };
    })(),
    (() => {
      let phase = 'generating';
      return { name: 'Generating shows attempted/total with a matching bar, and the review phase appears when the session completes', open: gold({
        'POST /api/goldstandard/generate': json(202, { session_id: SID, status: 'generating', pairs_total: 4, pairs_completed: 0 }),
        ['GET ' + SESSION]: () => json(200, phase === 'generating' ? session([pair('p1', 'pending')], { status: 'generating', pairs_total: 4, pairs_attempted: 1, pairs_completed: 1 }) : session(reviewPairs())) }, { fastPolls: true }), async run(s) {
        await waitSelected(s.page, 0);
        await press(s.page, 'Generate Pairs');
        await waitText(s.page, `Generating${ELLIPSIS} 1/4 pairs`);
        assert.equal(await s.page.evaluate(() => document.querySelector('.bg-blue-500.h-2').style.width), '25%');
        assert.equal(await bodyHas(s.page, 'Phase 2'), true);
        phase = 'done';
        await waitText(s.page, 'Generating', true);
        await waitText(s.page, 'Phase 2');
        assert.equal(await summary(s.page), `1 approved ${DOT} 1 edited ${DOT} 1 rejected ${DOT} 1 pending`);
      } };
    })(),
    { name: 'A refused generate shows the API message and leaves Generate enabled', open: gold({ 'POST /api/goldstandard/generate': apiError(409, 'GENERATION_BUSY', 'Synthetic generate refusal.') }), async run(s) {
      await waitSelected(s.page, 0);
      await press(s.page, 'Generate Pairs');
      await waitText(s.page, 'Synthetic generate refusal.');
      assert.equal((await buttonState(s.page, 'Generate Pairs')).disabled, false);
    } },
    { name: 'Generate with a session already shown asks first: Cancel sends nothing, OK starts a new one', open: Object.assign(gold({
        'POST /api/goldstandard/generate': json(202, { session_id: SID, status: 'completed', pairs_total: 4, pairs_completed: 4 }),
        ['GET ' + SESSION]: json(200, session(reviewPairs())) }), { onDialog: async d => { confirmAnswer ? await d.accept() : await d.dismiss(); } }), async run(s) {
      await loadSession(s.page);
      let dialogs = 0;
      s.page.on('dialog', d => { dialogs++; assert.match(d.message(), /Start a new session\?/); });
      confirmAnswer = false;
      await press(s.page, 'Generate Pairs');
      await waitFor(s.page, () => true, 'a tick');
      await new Promise(r => setTimeout(r, 300));
      assert.equal(dialogs, 1);
      assert.equal(callsTo(s, 'POST', '/api/goldstandard/generate').length, 0);
      confirmAnswer = true;
      await press(s.page, 'Generate Pairs');
      await waitFor(s.page, () => true, 'a tick');
      await new Promise(r => setTimeout(r, 300));
      assert.equal(dialogs, 2);
      assert.equal(callsTo(s, 'POST', '/api/goldstandard/generate').length, 1);
    } },
    { name: 'The summary bar counts approved, edited, rejected and pending pairs', open: review(), async run(s) {
      await loadSession(s.page);
      assert.equal(await summary(s.page), `1 approved ${DOT} 1 edited ${DOT} 1 rejected ${DOT} 1 pending`);
    } },
    { name: 'Approve and Reject send the pair status, update the row and the summary, and leave the row closed', open: review(), async run(s) {
      await loadSession(s.page);
      assert.equal(await pairControl(s.page, 'Question p1', 'Approve'), true);
      await waitFor(s.page, () => /2 approved .* 0 pending/.test(document.body.innerText), 'p1 approved');
      assert.equal(await statusOf(s.page, 'Question p1'), 'approved');
      assert.equal(await detailsOpen(s.page, 'Question p1'), false);
      assert.equal(await pairControl(s.page, 'Question p2', 'Reject'), true);
      await waitFor(s.page, () => /1 approved .* 2 rejected/.test(document.body.innerText.replace(/\s+/g, ' ')), 'p2 rejected');
      assert.equal(await statusOf(s.page, 'Question p2'), 'rejected');
      assert.equal(await detailsOpen(s.page, 'Question p2'), false);
      const patches = s.calls.filter(c => c.method === 'PATCH');
      assert.deepEqual(patches.map(c => [c.path, c.body]), [[SESSION + '/pair/p1', { status: 'approved' }], [SESSION + '/pair/p2', { status: 'rejected' }]]);
    } },
    { name: 'Regenerate sends the session and pair and replaces only that pair', open: review(), async run(s) {
      await loadSession(s.page);
      assert.equal(await pairControl(s.page, 'Question p3', 'Regenerate'), true);
      await waitText(s.page, 'Regenerated question');
      assert.equal(await bodyHas(s.page, 'Question p3'), false);
      for (const q of ['Question p1', 'Question p2', 'Question p4']) assert.equal(await bodyHas(s.page, q), true, q);
      const posts = callsTo(s, 'POST', '/api/goldstandard/regenerate');
      assert.equal(posts.length, 1);
      assert.deepEqual(posts[0].body, { session_id: SID, pair_id: 'p3' });
      assert.equal(await detailsOpen(s.page, 'Regenerated question'), false);
    } },
    { name: 'Edit opens the pair, Cancel sends nothing and Save sends status edited with the three fields', open: review(), async run(s) {
      await loadSession(s.page);
      assert.equal(await pairControl(s.page, 'Question p1', 'Edit'), true);
      await waitText(s.page, 'Edit Pair');
      assert.deepEqual(await s.page.evaluate(() => [...document.querySelectorAll('.fixed textarea')].map(t => t.value)), ['Question p1', 'Answer p1', 'Truth p1']);
      await press(s.page, 'Cancel');
      await waitText(s.page, 'Edit Pair', true);
      assert.equal(s.calls.filter(c => c.method === 'PATCH').length, 0);
      assert.equal(await pairControl(s.page, 'Question p1', 'Edit'), true);
      await waitText(s.page, 'Edit Pair');
      await fillNth(s.page, '.fixed textarea', 0, 'Edited question');
      await fillNth(s.page, '.fixed textarea', 1, 'Edited answer');
      await fillNth(s.page, '.fixed textarea', 2, 'Edited truth');
      await press(s.page, 'Save');
      await waitText(s.page, 'Edited question');
      const patches = s.calls.filter(c => c.method === 'PATCH');
      assert.deepEqual(patches.map(c => c.body), [{ status: 'edited', question: 'Edited question', answer: 'Edited answer', ground_truth: 'Edited truth' }]);
      assert.equal(await statusOf(s.page, 'Edited question'), 'edited');
      assert.equal(await summary(s.page), `1 approved ${DOT} 2 edited ${DOT} 1 rejected ${DOT} 0 pending`);
    } },
    { name: 'A failed review action shows the API message and keeps the pair as it was', open: review({ ['PATCH ' + SESSION + '/pair/*']: apiError(500, 'INTERNAL_ERROR', 'Synthetic patch failure.') }), async run(s) {
      await loadSession(s.page);
      assert.equal(await pairControl(s.page, 'Question p1', 'Approve'), true);
      await waitText(s.page, 'Synthetic patch failure.');
      assert.equal(await statusOf(s.page, 'Question p1'), 'pending');
      assert.equal(await summary(s.page), `1 approved ${DOT} 1 edited ${DOT} 1 rejected ${DOT} 1 pending`);
    } },
    { name: 'Export Approved is enabled only while some pair is approved or edited', open: review({}, [pair('p1', 'pending'), pair('p4', 'rejected')]), async run(s) {
      await loadSession(s.page);
      assert.equal((await buttonState(s.page, 'Export Approved')).disabled, true);
    } },
    { name: 'Export Approved is enabled when a pair is approved', open: review(), async run(s) {
      await loadSession(s.page);
      assert.equal((await buttonState(s.page, 'Export Approved')).disabled, false);
    } },
  ];
}

// -- Transfer: export and import controls ------------------------------------
const PKG_OK = 'ragpkg-vfyuia-20261021-0000-abcd1234.tar.gz', PKG_BAD = 'ragpkg-broken-20261021-0000-ffff0000.tar.gz';
const PACKAGES = [
  { filename: PKG_OK, size_bytes: 2048, collection: A, chunk_count: 3, fidelity: 'with-sources', created_at: null, readable: true },
  { filename: PKG_BAD, size_bytes: 10, collection: null, chunk_count: null, fidelity: null, created_at: null, readable: false },
];
const exportJob = (status, extra = {}) => json(200, { job_id: 'vfyexp', status, collection: A, chunks_written: 0, filename: null, size_bytes: null, source_document_count: null, fidelity: null, models_bundled: null, retrieve_script: null, warnings: [], error: null, error_code: null, error_detail: null, ...extra });
const importJob = (status, extra = {}) => json(200, { job_id: 'vfyimp', status, filename: PKG_OK, on_conflict: 'abort', collection: null, original_collection: null, chunks_written: 0, fidelity: null, renamed: false, notes: [], error: null, error_code: null, error_detail: null, ...extra });
const optionTexts = (page, i) => page.evaluate(n => [...document.querySelectorAll('select')[n].options].map(o => o.textContent.trim()), i);

function transferCases() {
  const xfer = (routes = {}) => ({ role: 'developer', path: '/transfer', fastPolls: true, routes: common(routes) });
  const pkgs = { 'GET /api/packages': json(200, { packages: PACKAGES }) };
  return [
    { name: 'Export lists each collection with its chunk count, selects the first and looks up the fidelity of a newly chosen one', open: xfer(), async run(s) {
      await waitSelected(s.page, 0);
      assert.deepEqual(await optionTexts(s.page, 0), [`${A} (12 chunks)`, `${B} (0 chunks)`]);
      assert.equal(await s.page.evaluate(() => document.querySelector('select').value), A);
      await waitFor(s.page, () => true, 'a tick');
      assert.equal(callsTo(s, 'GET', '/api/tune/' + A).length, 1);
      await fillNth(s.page, 'select', 0, B);
      await waitFor(s.page, () => true, 'a tick');
      await new Promise(r => setTimeout(r, 200));
      assert.equal(callsTo(s, 'GET', '/api/tune/' + B).length, 1);
    } },
    (() => {
      const h = hold();
      return { name: 'The fidelity line reads "reading" while looked up, then names with-sources and the documents that travel', open: xfer({ 'GET /api/tune/*': () => h }), async run(s) {
        await waitText(s.page, `Fidelity: reading${ELLIPSIS}`);
        h.release(json(200, tune(A)));
        await waitText(s.page, `Fidelity: with-sources ${DASH} 2 original document(s) will travel with the package.`);
        assert.equal(await bodyHas(s.page, 'reading'), false);
      } };
    })(),
    { name: 'A metadata-only collection says no original documents were kept', open: xfer({ 'GET /api/tune/*': call => json(200, tune(call.path.split('/').pop(), 'metadata-only')) }), async run(s) {
      await waitText(s.page, `Fidelity: metadata-only ${DASH} No original documents were retained, so the package cannot be re-chunked after import.`);
    } },
    { name: 'The include-models box starts unchecked and its state is sent with the export', open: xfer({
        'POST /api/export': json(202, { job_id: 'vfyexp', status: 'queued', collection: A }),
        'GET /api/export/job/vfyexp': exportJob('completed', { chunks_written: 3, filename: 'ragpkg-vfy.tar.gz', size_bytes: 1000, fidelity: 'with-sources', models_bundled: false, retrieve_script: true }) }), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal(await s.page.evaluate(() => document.querySelector('input[type=checkbox]').checked), false);
      await press(s.page, 'Export');
      await waitText(s.page, 'Done.');
      await s.page.click('input[type=checkbox]');
      await press(s.page, 'Export');
      await waitFor(s.page, () => true, 'a tick');
      await waitFor(s.page, () => window.__forms !== undefined, 'page');
      await new Promise(r => setTimeout(r, 300));
      const posts = callsTo(s, 'POST', '/api/export');
      assert.deepEqual(posts.map(c => c.body), [{ collection: A, include_models: false }, { collection: A, include_models: true }]);
    } },
    (() => {
      let phase = 'running';
      return { name: 'A running export shows its chunk count with the button busy, then the file, size and options when done, plus warnings', open: xfer({
        'POST /api/export': json(202, { job_id: 'vfyexp', status: 'queued', collection: A }),
        'GET /api/export/job/vfyexp': () => (phase === 'running' ? exportJob('running', { chunks_written: 5 })
          : exportJob('completed', { chunks_written: 9, filename: 'ragpkg-vfy.tar.gz', size_bytes: 2500000, fidelity: 'with-sources', models_bundled: true, retrieve_script: true, warnings: ['Synthetic export warning.'] })) }), async run(s) {
        await waitSelected(s.page, 0);
        await press(s.page, 'Export');
        await waitText(s.page, `Writing${ELLIPSIS} 5 chunks so far.`);
        assert.equal((await buttonState(s.page, `Exporting${ELLIPSIS}`)).disabled, true);
        phase = 'done';
        await waitText(s.page, 'Done.');
        await waitText(s.page, 'ragpkg-vfy.tar.gz');
        assert.equal(await bodyHas(s.page, '(2.5 MB, with-sources, models included, with retrieve.py)'), true);
        assert.equal(await bodyHas(s.page, 'Synthetic export warning.'), true);
        assert.equal((await buttonState(s.page, 'Export')).disabled, false);
      } };
    })(),
  ];
}

function packageCases() {
  const xfer = (routes = {}) => ({ role: 'developer', path: '/transfer', fastPolls: true, routes: common(routes) });
  const withPkgs = (routes = {}) => xfer({ 'GET /api/packages': json(200, { packages: PACKAGES }), ...routes });
  const pick = (page, name) => fillNth(page, 'select', 1, name);
  const start = async (page, name = PKG_OK) => { await waitFor(page, n => [...document.querySelectorAll('select')[1].options].length > 1, 'the packages'); await pick(page, name); await press(page, 'Import'); };
  return [
    { name: 'A readable package shows its collection, chunks, fidelity and size; an unreadable one says so', open: withPkgs(), async run(s) {
      await waitFor(s.page, () => document.querySelectorAll('select')[1]?.options.length > 1, 'the packages');
      assert.deepEqual(await optionTexts(s.page, 1), ['Select a package' + ELLIPSIS, `${PKG_OK} ${DASH} ${A}, 3 chunks, with-sources, 2 KB`, `${PKG_BAD} ${DASH} unreadable`]);
      assert.equal(await bodyHas(s.page, 'Nothing in ./exports yet'), false);
    } },
    (() => {
      let lists = 0;
      return { name: 'With no packages the empty hint shows, and Refresh asks again and lists what appeared', open: xfer({ 'GET /api/packages': () => json(200, { packages: ++lists === 1 ? [] : PACKAGES }) }), async run(s) {
        await waitText(s.page, 'Nothing in ./exports yet. Copy a package there and press Refresh.');
        await press(s.page, 'Refresh');
        await waitText(s.page, 'Nothing in ./exports yet', true);
        assert.equal(callsTo(s, 'GET', '/api/packages').length, 2);
        assert.equal((await optionTexts(s.page, 1)).length, 3);
      } };
    })(),
    { name: 'Import is disabled until a package is chosen; Abort is the default conflict choice and the chosen one is sent', open: withPkgs({
        'POST /api/import': json(202, { job_id: 'vfyimp', status: 'queued', filename: PKG_OK }),
        'GET /api/import/job/vfyimp': importJob('completed', { collection: A + '_2', original_collection: A, renamed: true, chunks_written: 3, fidelity: 'with-sources', notes: ['Synthetic import note.'] }) }), async run(s) {
      await waitFor(s.page, () => document.querySelectorAll('select')[1]?.options.length > 1, 'the packages');
      assert.equal((await buttonState(s.page, 'Import')).disabled, true);
      assert.equal(await s.page.evaluate(() => document.querySelector('input[name=conflict]:checked').value), 'abort');
      await pick(s.page, PKG_OK);
      assert.equal((await buttonState(s.page, 'Import')).disabled, false);
      await s.page.click('input[name=conflict][value=rename]');
      await press(s.page, 'Import');
      await waitText(s.page, `Imported as ${A}_2 (renamed from ${A}) ${DASH} 3 chunks, with-sources`);
      assert.equal(await bodyHas(s.page, 'Synthetic import note.'), true);
      const posts = callsTo(s, 'POST', '/api/import');
      assert.deepEqual(posts.map(c => c.body), [{ filename: PKG_OK, on_conflict: 'rename' }]);
    } },
    { name: 'A failed import shows Failed, its error code and message, and Import is enabled again', open: withPkgs({
        'POST /api/import': json(202, { job_id: 'vfyimp', status: 'queued', filename: PKG_OK }),
        'GET /api/import/job/vfyimp': importJob('failed', { error_code: 'PACKAGE_CORRUPT', error: 'Synthetic package failure.' }) }), async run(s) {
      await start(s.page);
      await waitText(s.page, 'Failed.');
      assert.equal(await s.page.evaluate(() => document.querySelector('p.text-red-600 code')?.textContent.trim()), 'PACKAGE_CORRUPT');
      assert.equal(await bodyHas(s.page, 'Synthetic package failure.'), true);
      assert.equal((await buttonState(s.page, 'Import')).disabled, false);
    } },
    { name: 'An import the API refuses to start shows its message and leaves Import enabled', open: withPkgs({ 'POST /api/import': apiError(409, 'COLLECTION_EXISTS', 'Synthetic import refusal.') }), async run(s) {
      await start(s.page);
      await waitText(s.page, 'Synthetic import refusal.');
      assert.equal((await buttonState(s.page, 'Import')).disabled, false);
      assert.equal(callsTo(s, 'GET', '/api/import/job/vfyimp').length, 0);
    } },
  ];
}

function helpCases() {
  return [
    { name: 'A failed help request shows the reason and a hint, with no Loading text left behind', open: { role: 'developer', path: '/help/transfer', routes: common({ 'GET /api/help/transfer': apiError(500, 'INTERNAL_ERROR', 'Synthetic help failure.') }) }, async run(s) {
      await waitText(s.page, 'Could not load the help content (Synthetic help failure.). The API may be starting up.');
      assert.equal(await bodyHas(s.page, `Loading${ELLIPSIS}`), false);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('article').length), 0);
    } },
    { name: 'A proxy error page on the help request is shown as its readable message', open: { role: 'developer', path: '/help/transfer', routes: common({ 'GET /api/help/transfer': html(502, 'Bad Gateway') }) }, async run(s) {
      await waitText(s.page, 'Could not load the help content (The API is not responding. It may still be starting; try again in a minute.). The API may be starting up.');
    } },
    { name: 'The help page and the Transfer page link to each other', open: { role: 'developer', path: '/help/transfer', routes: common() }, async run(s) {
      await waitText(s.page, 'Some text.');
      await press(s.page, `Go to Transfer ${ARROW}`, 'a');
      await waitPath(s.page, '/transfer');
      await waitText(s.page, 'Export a collection');
      await press(s.page, `How export and import work ${ARROW}`, 'a');
      await waitPath(s.page, '/help/transfer');
      await waitText(s.page, 'Some text.');
    } },
  ];
}

// -- Health: latency stat row, charts and empty state ------------------------
const health = json(200, { status: 'ok', services: { weaviate: { status: 'ok', latency_ms: 5 }, ollama: { llm: { status: 'ok', latency_ms: 7, model: 'vfy-llm' }, embed: { status: 'ok', latency_ms: 9, model: 'vfy-embed' } } } });
const record = (i, r, l) => ({ timestamp: `2026-10-21T10:0${i}:00Z`, collection: A, retrieval_mode: 'hnsw', retrieval_ms: r, llm_ms: l, total_ms: r + l });
const metrics = (records, stats = {}) => json(200, { total_records: records.length, retrieval_latency: { p50: 10.4, p95: 20.6, p99: 30.2 }, llm_latency: { p50: 100.2, p95: 200.7, p99: 300.4 }, total_latency: { p50: 110.7, p95: 221.1, p99: 331.9 }, history: records, ...stats });

function healthCases() {
  const open = m => ({ role: 'engineer', path: '/health', routes: common({ 'GET /api/health': health, 'GET /api/metrics/latency': m, 'GET /api/goldstandard/diagnostics': json(200, { issues: [] }) }) });
  return [
    { name: 'Health shows the latency trend heading, a P50/P95/P99 row per stage in whole ms, and a chart with its three series', open: open(metrics([record(1, 10, 100), record(2, 12, 110), record(3, 14, 120)])), async run(s) {
      await waitText(s.page, 'Latency Trends (3 queries in ring buffer)');
      const boxes = await s.page.evaluate(() => [...document.querySelectorAll('.border.rounded.p-3')].map(b => b.innerText.replace(/\s+/g, ' ').trim()));
      assert.deepEqual(boxes, [`RETRIEVAL P50: 10ms ${DOT} P95: 21ms ${DOT} P99: 30ms`, `LLM P50: 100ms ${DOT} P95: 201ms ${DOT} P99: 300ms`, `TOTAL P50: 111ms ${DOT} P95: 221ms ${DOT} P99: 332ms`]);
      await waitFor(s.page, () => document.querySelectorAll('.recharts-line').length === 3, 'three chart lines');
      assert.deepEqual(await s.page.evaluate(() => [...document.querySelectorAll('.recharts-legend-item-text')].map(t => t.textContent.trim()).sort()), ['LLM', 'Retrieval', 'Total']);
      assert.equal(await bodyHas(s.page, 'No query data yet'), false);
    } },
    { name: 'Health with no queries recorded shows the empty message and no chart', open: open(metrics([], { total_records: 0 })), async run(s) {
      await waitText(s.page, 'No query data yet. Run some Q&A queries to populate charts.');
      assert.equal(await bodyHas(s.page, 'Latency Trends'), false);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('.recharts-wrapper').length), 0);
      assert.equal(await bodyHas(s.page, 'RETRIEVAL'), false);
    } },
  ];
}

async function runUiPageTests(browser, base, reporter) {
  const run = (section, cases) => S.runCases(browser, base, reporter, section, cases);
  await run('Gold Standard generate and review actions (#400)', goldCases());
  await run('Transfer export controls (#400)', transferCases());
  await run('Transfer package list and import (#400)', packageCases());
  await run('Help page error state and cross-links (#400)', helpCases());
  await run('Health latency stat row, chart and empty state (#400)', healthCases());
}

module.exports = { runUiPageTests };
if (require.main === module) (async () => {
  const { makeReporter } = require('./lib');
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runUiPageTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
