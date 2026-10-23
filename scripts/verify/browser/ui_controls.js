// UI controls that had no browser check (#400): Collections, Import, navigation
// and roles, Q&A, Retrieval, and the client's handling of the proxy's own error
// pages. Every API call is answered by the check (see stub.js), so nothing here
// touches the backend or a model. Non-ASCII UI text is written as \u escapes.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const S = require('./stub');
const { json, apiError, html, hold, callsTo, text, waitFor, waitText, press, fill, fillNth, forms, buttonState, nav, waitPath } = S;

const A = 'VfyUiA', B = 'VfyUiB';
const DASH = '\u2014', DOT = '\u00b7', ELLIPSIS = '\u2026', CROSS = '\u2715';
const COLS = [
  { name: A, object_count: 12, index_type: 'hnsw', distance_metric: 'cosine', created_at: '2026-01-02T12:00:00Z', hnsw_config: { ef: 64, efConstruction: 128, maxConnections: 64 } },
  { name: B, object_count: 0, index_type: 'flat', distance_metric: 'dot', created_at: null, hnsw_config: null },
];
const retrievalConfig = (name, over = {}) => ({ collection: name, retrieval_mode: 'hnsw', top_k: 5, alpha: 0.75, ef: null, response_format: 'engineer', is_default: true, ...over });
const queryReply = (over = {}) => ({ answer: 'A plain answer.', citations: null, retrieval_latency_ms: 12, llm_latency_ms: 34, chunks_retrieved: 2, ...over });
const CITATIONS = [
  { source_file: 'alpha.txt', chunk_index: 2, score: 0.12345, excerpt: 'First excerpt.' },
  { source_file: 'beta.txt', chunk_index: 0, score: null, excerpt: 'Second excerpt.' },
];
// Answers for the pages a case may pass through; a case adds or overrides routes.
const base = (extra = {}) => ({
  'GET /api/collections': json(200, { collections: COLS }),
  'GET /api/retrieval/config/*': call => json(200, retrievalConfig(call.path.split('/').pop())),
  'GET /api/packages': json(200, { packages: [] }),
  'GET /api/tune/*': call => json(200, { collection: call.path.split('/').pop(), fidelity: 'with-sources', source_document_count: 2, can_rechunk: true, can_reembed: true, can_reindex: true, note: '' }),
  'GET /api/help/transfer': json(200, { topic: 'transfer', markdown: '# Help\n\nSome text.' }),
  ...extra,
});

const bodyHas = (page, want) => page.evaluate(w => document.body.innerText.replace(/\s+/g, ' ').includes(w), want);
const labels = page => page.evaluate(() => [...document.querySelectorAll('label')].map(l => l.textContent.trim()));
const rangeAttrs = (page, i) => page.evaluate(n => { const r = document.querySelectorAll('input[type=range]')[n]; return r ? { min: r.min, max: r.max, step: r.step, value: r.value } : null; }, i);
const selectOptions = (page, i) => page.evaluate(n => [...(document.querySelectorAll('select')[n]?.options || [])].map(o => o.value), i);
const selected = (page, i) => page.evaluate(n => document.querySelectorAll('select')[n]?.value, i);
const modalOpen = page => page.evaluate(() => !!document.querySelector('.fixed.inset-0'));
const waitSelected = (page, i, why) => waitFor(page, n => !!document.querySelectorAll('select')[n]?.value, why || 'the collection select to be filled', i);

function tempFiles(names) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'vfyui-'));
  return names.map(n => { const f = path.join(dir, n); fs.writeFileSync(f, 'inert test file\n'); return f; });
}
async function queueFiles(page, names) {
  const input = await page.$('#file-input');
  await input.uploadFile(...tempFiles(names));
  await waitText(page, names[names.length - 1]);
}

// Ctrl+Enter in the question box.
async function ctrlEnter(page) {
  await page.focus('textarea');
  await page.keyboard.down('Control'); await page.keyboard.press('Enter'); await page.keyboard.up('Control');
}
async function askQuestion(page, question = 'What is inert?') {
  await waitSelected(page, 0);
  await fill(page, 'textarea', question);
  await ctrlEnter(page);
}
const qaOpen = (role, routes = {}) => ({ role, path: '/qa', routes: base({ 'POST /api/query': json(200, queryReply()), ...routes }) });

function collectionsCases() {
  const open = (routes = {}) => ({ role: 'engineer', path: '/collections', routes: base(routes) });
  return [
    { name: 'Collections table shows its columns, each collection and a dash for a null created date', open: open(), async run(s) {
      await waitText(s.page, A);
      const heads = await s.page.evaluate(() => [...document.querySelectorAll('thead th')].map(t => t.textContent.trim()));
      assert.deepEqual(heads, ['Name', 'Objects', 'Index', 'Distance', 'Created', '']);
      const rows = await s.page.evaluate(() => [...document.querySelectorAll('tbody tr')].map(r => [...r.querySelectorAll('td')].map(c => c.textContent.trim())));
      const date = await s.page.evaluate(v => new Date(v).toLocaleDateString(), COLS[0].created_at);
      assert.deepEqual(rows[0].slice(0, 5), [A, '12', 'hnsw', 'cosine', date]);
      assert.notEqual(date, DASH);
      assert.deepEqual(rows[1].slice(0, 5), [B, '0', 'flat', 'dot', DASH]);
    } },
    { name: 'New Collection modal starts with HNSW, Cosine and the three HNSW sliders at their defaults and bounds', open: open(), async run(s) {
      await waitText(s.page, A);
      await press(s.page, '+ New Collection');
      await waitFor(s.page, () => !!document.querySelector('.fixed.inset-0'), 'the modal');
      assert.equal(await s.page.evaluate(() => document.querySelector('.fixed input:not([type=range])').value), '');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('.fixed select')[0].value), 'hnsw');
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('.fixed select')[1].value), 'cosine');
      assert.deepEqual(await rangeAttrs(s.page, 0), { min: '64', max: '512', step: '8', value: '128' });
      assert.deepEqual(await rangeAttrs(s.page, 1), { min: '16', max: '128', step: '4', value: '64' });
      assert.deepEqual(await rangeAttrs(s.page, 2), { min: '16', max: '512', step: '8', value: '64' });
      const l = await labels(s.page);
      for (const want of ['efConstruction: 128', 'maxConnections: 64', 'ef: 64']) assert.ok(l.includes(want), `label ${want} in ${JSON.stringify(l)}`);
      await fillNth(s.page, 'input[type=range]', 0, 256);
      assert.ok((await labels(s.page)).includes('efConstruction: 256'));
    } },
    { name: 'Choosing Flat removes the HNSW sliders and choosing HNSW brings them back', open: open(), async run(s) {
      await waitText(s.page, A);
      await press(s.page, '+ New Collection');
      await fillNth(s.page, '.fixed select', 0, 'flat');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 0, 'the sliders to go');
      await fillNth(s.page, '.fixed select', 0, 'hnsw');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 3, 'the sliders to return');
    } },
    { name: 'Create sends the typed name, the chosen index, distance and slider values, then closes and reloads the list', open: (() => {
      let lists = 0;
      return open({ 'GET /api/collections': () => { lists++; return json(200, { collections: lists === 1 ? COLS : [...COLS, { name: 'VfyUiNew', object_count: 0, index_type: 'hnsw', distance_metric: 'dot', created_at: null, hnsw_config: null }] }); },
                    'POST /api/collections': json(201, { name: 'VfyUiNew' }) });
    })(), async run(s) {
      await waitText(s.page, A);
      await press(s.page, '+ New Collection');
      await fill(s.page, '.fixed input:not([type=range])', 'VfyUiNew');
      await fillNth(s.page, '.fixed select', 1, 'dot');
      await fillNth(s.page, 'input[type=range]', 0, 256);
      await fillNth(s.page, 'input[type=range]', 1, 32);
      await fillNth(s.page, 'input[type=range]', 2, 96);
      await press(s.page, 'Create');
      await waitText(s.page, 'VfyUiNew');
      const posts = callsTo(s, 'POST', '/api/collections');
      assert.equal(posts.length, 1);
      assert.deepEqual(posts[0].body, { name: 'VfyUiNew', index_type: 'hnsw', distance_metric: 'dot', hnsw_config: { efConstruction: 256, maxConnections: 32, ef: 96 } });
      assert.equal(await modalOpen(s.page), false);
      assert.equal(callsTo(s, 'GET', '/api/collections').length, 2);
    } },
  ];
}

function importCases() {
  const open = (role = 'engineer', routes = {}) => ({ role, path: '/import', routes: base(routes) });
  const strategyIs = (page, value) => fillNth(page, 'select', 1, value);
  const ready = async page => { await waitSelected(page, 0); };
  return [
    { name: 'Import queued list shows each file, removes only the one chosen, and Start Ingest follows the queue', open: open(), async run(s) {
      await ready(s.page);
      assert.equal((await buttonState(s.page, 'Start Ingest')).disabled, true);
      await queueFiles(s.page, ['one.txt', 'two.txt']);
      const names = () => s.page.evaluate(() => [...document.querySelectorAll('ul li span')].map(x => x.textContent.trim()));
      assert.deepEqual(await names(), ['one.txt', 'two.txt']);
      assert.equal((await buttonState(s.page, 'Start Ingest')).disabled, false);
      await press(s.page, CROSS);
      await waitFor(s.page, () => document.querySelectorAll('ul li').length === 1, 'one file removed');
      assert.deepEqual(await names(), ['two.txt']);
      await press(s.page, CROSS);
      await waitFor(s.page, () => document.querySelectorAll('ul li').length === 0, 'no files left');
      assert.equal((await buttonState(s.page, 'Start Ingest')).disabled, true);
    } },
    { name: 'Import page lists the collections, selects the first and shows the default sliders', open: open(), async run(s) {
      await ready(s.page);
      assert.deepEqual(await selectOptions(s.page, 0), [A, B]);
      assert.equal(await selected(s.page, 0), A);
      const l = await labels(s.page);
      for (const want of ['Chunk Size: 1000', 'Overlap: 200', 'Min Chunk Size: 100']) assert.ok(l.includes(want), `${want} in ${JSON.stringify(l)}`);
      assert.ok(!l.some(x => x.startsWith('Similarity Threshold')));
    } },
    { name: 'Engineer sees Overlap for fixed, overlap and language, and Similarity Threshold only for semantic', open: open(), async run(s) {
      await ready(s.page);
      const has = async prefix => (await labels(s.page)).some(x => x.startsWith(prefix));
      for (const strategy of ['fixed', 'overlap', 'language']) {
        await strategyIs(s.page, strategy);
        await waitText(s.page, strategy === 'language' ? 'natural sentence' : strategy === 'fixed' ? 'equal-sized' : 'shares some text');
        assert.equal(await has('Overlap:'), true, strategy + ' shows Overlap');
        assert.equal(await has('Similarity Threshold'), false, strategy + ' hides Similarity');
      }
      await strategyIs(s.page, 'context_aware');
      await waitText(s.page, 'headings, paragraphs');
      assert.equal(await has('Overlap:'), false);
      assert.equal(await has('Similarity Threshold'), false);
      await strategyIs(s.page, 'semantic');
      await waitText(s.page, 'same topic');
      assert.equal(await has('Overlap:'), false);
      assert.equal(await has('Similarity Threshold: 0.85'), true);
    } },
    { name: 'Developer never sees Similarity Threshold, not even for semantic', open: open('developer'), async run(s) {
      await ready(s.page);
      await strategyIs(s.page, 'semantic');
      await waitText(s.page, 'same topic');
      assert.equal((await labels(s.page)).some(x => x.startsWith('Similarity Threshold')), false);
      assert.equal((await labels(s.page)).some(x => x.startsWith('Overlap:')), false);
    } },
    { name: 'Lowering Chunk Size pulls Overlap down to Chunk Size minus 50 and caps the Overlap slider', open: open(), async run(s) {
      await ready(s.page);
      await fillNth(s.page, 'input[type=range]', 0, 150);
      await waitFor(s.page, () => [...document.querySelectorAll('label')].some(l => l.textContent.trim() === 'Overlap: 100'), 'Overlap 100');
      assert.ok((await labels(s.page)).includes('Chunk Size: 150'));
      assert.equal((await rangeAttrs(s.page, 1)).max, '100');
      await fillNth(s.page, 'input[type=range]', 0, 3000);
      await waitFor(s.page, () => [...document.querySelectorAll('label')].some(l => l.textContent.trim() === 'Chunk Size: 3000'), 'Chunk Size 3000');
      assert.equal((await rangeAttrs(s.page, 1)).max, '2000');
      assert.ok((await labels(s.page)).includes('Overlap: 100'), 'overlap is not raised again');
    } },
    { name: 'Start Ingest sends the collection, strategy and slider values as the form fields', open: open('engineer', {
      'POST /api/ingest/upload': json(202, { job_id: 'vfyjob', status: 'queued', files_queued: 1, collection: B }),
      'GET /api/ingest/job/*': json(200, { job_id: 'vfyjob', status: 'completed', files_total: 1, files_completed: 1, files_failed: 0, chunks_stored: 1, errors: [] }) }), async run(s) {
      await ready(s.page);
      await queueFiles(s.page, ['doc.txt']);
      await fillNth(s.page, 'select', 0, B);
      await strategyIs(s.page, 'semantic');
      await waitText(s.page, 'same topic');
      await fillNth(s.page, 'input[type=range]', 0, 2000);
      await fillNth(s.page, 'input[type=range]', 1, 40);
      await fillNth(s.page, 'input[type=range]', 2, 0.5);
      await press(s.page, 'Start Ingest');
      await waitFor(s.page, () => window.__forms.length === 1, 'the upload');
      const [form] = await forms(s.page);
      const get = k => form.filter(([n]) => n === k).map(([, v]) => v);
      assert.deepEqual(get('files'), ['file:doc.txt']);
      assert.deepEqual(get('collection'), [B]);
      assert.deepEqual(get('strategy'), ['semantic']);
      assert.deepEqual(get('chunk_size'), ['2000']);
      assert.deepEqual(get('min_chunk_size'), ['40']);
      assert.deepEqual(get('similarity_threshold'), ['0.5']);
      assert.deepEqual(get('chunk_overlap'), ['200']);
    } },
    { name: 'Import "+ New" modal ignores a blank name, creates with cosine and the default HNSW values, selects the new collection, and Cancel sends nothing', open: (() => {
      let lists = 0;
      return open('engineer', { 'GET /api/collections': () => { lists++; return json(200, { collections: lists === 1 ? COLS : [...COLS, { name: 'VfyUiNew', object_count: 0, index_type: 'flat', distance_metric: 'cosine', created_at: null, hnsw_config: null }] }); },
                                'POST /api/collections': json(201, { name: 'VfyUiNew' }) });
    })(), async run(s) {
      await ready(s.page);
      await press(s.page, '+ New');
      await waitFor(s.page, () => !!document.querySelector('.fixed.inset-0'), 'the modal');
      await press(s.page, 'Create');
      await fill(s.page, '.fixed input', '   ');
      await press(s.page, 'Create');
      assert.equal(callsTo(s, 'POST', '/api/collections').length, 0);
      assert.equal(await modalOpen(s.page), true);
      await press(s.page, 'Cancel');
      assert.equal(await modalOpen(s.page), false);
      assert.equal(callsTo(s, 'POST', '/api/collections').length, 0);
      await press(s.page, '+ New');
      await fill(s.page, '.fixed input', 'VfyUiNew');
      await fill(s.page, '.fixed select', 'flat');
      await press(s.page, 'Create');
      await waitFor(s.page, () => document.querySelectorAll('select')[0].value === 'VfyUiNew', 'the new collection selected');
      const posts = callsTo(s, 'POST', '/api/collections');
      assert.equal(posts.length, 1);
      assert.deepEqual(posts[0].body, { name: 'VfyUiNew', index_type: 'flat', distance_metric: 'cosine', hnsw_config: { efConstruction: 128, maxConnections: 64, ef: 64 } });
      assert.equal(await modalOpen(s.page), false);
    } },
  ];
}

function navigationCases() {
  const gated = ['/import', '/chunking', '/retrieval', '/goldstandard', '/transfer', '/help/transfer'];
  const badge = { engineer: 'AI Engineer', developer: 'Developer', end_user: 'End User' };
  const goto = async (page, p) => { await page.goto(S.BASE + p, { waitUntil: 'domcontentloaded' }); };
  return [
    { name: 'Developer nav has Q&A, Import, Chunking, Retrieval, Gold Standard and Transfer, and no Collections or Health', open: { role: 'developer', path: '/qa', routes: base() }, async run(s) {
      await waitFor(s.page, () => document.querySelectorAll('nav a').length > 0, 'the nav');
      assert.deepEqual(await nav(s.page), ['Q&A', 'Import', 'Chunking', 'Retrieval', 'Gold Standard', 'Transfer']);
    } },
    { name: 'Developer is sent back to Q&A from /collections and /health, but can open /transfer and /help/transfer', open: { role: 'developer', path: '/qa', routes: base() }, async run(s) {
      for (const p of ['/collections', '/health']) {
        await goto(s.page, p);
        await waitText(s.page, 'Ask a Question');
        await waitPath(s.page, '/qa');
      }
      await goto(s.page, '/transfer');
      await waitText(s.page, 'Export a collection');
      await waitPath(s.page, '/transfer');
      await goto(s.page, '/help/transfer');
      await waitText(s.page, 'Export and Import');
      await waitPath(s.page, '/help/transfer');
    } },
    { name: 'End User opening a Developer page directly is sent to Q&A (gating is client-side only until #25)', open: { role: 'end_user', path: '/qa', routes: base() }, async run(s) {
      for (const p of [...gated, '/collections', '/health']) {
        await goto(s.page, p);
        await waitText(s.page, 'Ask a Question');
        await waitPath(s.page, '/qa');
      }
    } },
    ...['engineer', 'developer', 'end_user'].map(role => ({ name: `Choosing ${badge[role]} on the landing page opens /qa with that role badge and stores the role`, open: { role: null, path: '/', routes: base() }, async run(s) {
      await waitText(s.page, 'Select your role to continue');
      assert.equal(await s.page.evaluate(label => {
        const b = [...document.querySelectorAll('button')].find(x => x.querySelector('h2')?.textContent.trim() === label);
        if (!b) return false; b.click(); return true;
      }, badge[role]), true);
      await waitPath(s.page, '/qa');
      await waitText(s.page, 'Ask a Question');
      assert.equal(await s.page.evaluate(() => document.querySelector('nav span.bg-blue-100')?.textContent.trim()), badge[role]);
      assert.deepEqual(JSON.parse(await s.page.evaluate(() => sessionStorage.getItem('rag_role'))), { role });
    } })),
    { name: 'Switch Role returns to the role chooser, forgets the role, and a later /qa visit goes back to it', open: { role: 'developer', path: '/qa', routes: base() }, async run(s) {
      await waitText(s.page, 'Ask a Question');
      await press(s.page, 'Switch Role');
      await waitPath(s.page, '/');
      await waitText(s.page, 'Select your role to continue');
      assert.equal(await s.page.evaluate(() => sessionStorage.getItem('rag_role')), null);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('nav').length), 0);
      await goto(s.page, '/qa');
      await waitText(s.page, 'Select your role to continue');
      await waitPath(s.page, '/');
    } },
  ];
}

function qaCases() {
  return [
    { name: 'Ask is disabled for empty and blank questions and enabled for text', open: qaOpen('engineer'), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal((await buttonState(s.page, 'Ask')).disabled, true);
      await fill(s.page, 'textarea', '   ');
      assert.equal((await buttonState(s.page, 'Ask')).disabled, true);
      await fill(s.page, 'textarea', 'a question');
      assert.equal((await buttonState(s.page, 'Ask')).disabled, false);
    } },
    { name: 'Ctrl+Enter submits the question once, and does nothing on an empty box', open: qaOpen('engineer'), async run(s) {
      await waitSelected(s.page, 0);
      await ctrlEnter(s.page);
      await new Promise(r => setTimeout(r, 300));
      assert.equal(callsTo(s, 'POST', '/api/query').length, 0);
      await fill(s.page, 'textarea', 'What is inert?');
      await ctrlEnter(s.page);
      await waitText(s.page, 'A plain answer.');
      const posts = callsTo(s, 'POST', '/api/query');
      assert.equal(posts.length, 1);
      assert.equal(posts[0].body.question, 'What is inert?');
      assert.equal(posts[0].body.collection, A);
    } },
    { name: 'Plain Enter inserts a newline and does not submit', open: qaOpen('engineer'), async run(s) {
      await waitSelected(s.page, 0);
      await s.page.focus('textarea');
      await s.page.keyboard.type('line one');
      await s.page.keyboard.press('Enter');
      await s.page.keyboard.type('line two');
      assert.equal(await s.page.evaluate(() => document.querySelector('textarea').value), 'line one\nline two');
      await new Promise(r => setTimeout(r, 300));
      assert.equal(callsTo(s, 'POST', '/api/query').length, 0);
    } },
    (() => {
      const h = hold();
      return { name: 'While a question is answered the button reads Thinking and is disabled, then Ask returns', open: qaOpen('engineer', { 'POST /api/query': () => h }), async run(s) {
        await askQuestion(s.page);
        await waitText(s.page, 'Thinking' + ELLIPSIS);
        assert.equal((await buttonState(s.page, 'Thinking' + ELLIPSIS)).disabled, true);
        h.release(json(200, queryReply()));
        await waitText(s.page, 'A plain answer.');
        assert.equal((await buttonState(s.page, 'Ask')).disabled, false);
      } };
    })(),
    { name: 'With citations off the query asks for none and no Sources panel shows, even if the reply carries some', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ citations: CITATIONS })) }), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal(await s.page.evaluate(() => document.querySelector('input[type=checkbox]').checked), false);
      await askQuestion(s.page);
      await waitText(s.page, 'A plain answer.');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.include_citations, false);
      assert.equal(await bodyHas(s.page, 'Sources'), false);
    },},
    { name: 'With citations on the query asks for them and the Sources panel lists file, chunk, score (n/a when null) and excerpt', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ citations: CITATIONS })) }), async run(s) {
      await waitSelected(s.page, 0);
      await s.page.click('input[type=checkbox]');
      await askQuestion(s.page);
      await waitText(s.page, 'Sources');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.include_citations, true);
      const panel = await s.page.evaluate(() => [...document.querySelectorAll('h3')].find(h => h.textContent.trim() === 'Sources')?.parentElement.innerText.replace(/\s+/g, ' '));
      for (const want of ['alpha.txt', `chunk 2 ${DOT} score 0.123`, 'First excerpt.', 'beta.txt', `chunk 0 ${DOT} score n/a`, 'Second excerpt.']) assert.ok(panel.includes(want), `${want} in ${panel}`);
    } },
    { name: 'Unchecking the citations box hides the panel of the answer on screen and checking it brings it back without a new query', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ citations: CITATIONS })) }), async run(s) {
      await waitSelected(s.page, 0);
      await s.page.click('input[type=checkbox]');
      await askQuestion(s.page);
      await waitText(s.page, 'Sources');
      await s.page.click('input[type=checkbox]');
      await waitText(s.page, 'Sources', true);
      await s.page.click('input[type=checkbox]');
      await waitText(s.page, 'Sources');
      assert.equal(callsTo(s, 'POST', '/api/query').length, 1);
    } },
    { name: 'The answer renders markdown and the latency line shows both times', open: qaOpen('engineer', { 'POST /api/query': json(200, queryReply({ answer: 'This is **bold words** here.\n\n- first item\n- second item', retrieval_latency_ms: 12, llm_latency_ms: 345 })) }), async run(s) {
      await askQuestion(s.page);
      await waitText(s.page, 'bold words');
      const dom = await s.page.evaluate(() => ({
        strong: [...document.querySelectorAll('.prose strong')].map(x => x.textContent),
        items: [...document.querySelectorAll('.prose li')].map(x => x.textContent.trim()),
      }));
      assert.deepEqual(dom.strong, ['bold words']);
      assert.deepEqual(dom.items, ['first item', 'second item']);
      assert.equal(await bodyHas(s.page, '**'), false);
      assert.equal(await bodyHas(s.page, `Retrieved in 12ms ${DOT} Generated in 345ms`), true);
    } },
    (() => {
      let n = 0;
      return { name: 'A failed query shows the API message with no answer or latency line, and asking again clears it', open: qaOpen('engineer', { 'POST /api/query': () => (++n === 1 ? apiError(500, 'INTERNAL_ERROR', 'Synthetic query failure.') : json(200, queryReply())) }), async run(s) {
        await askQuestion(s.page);
        await waitText(s.page, 'Synthetic query failure.');
        assert.equal(await bodyHas(s.page, 'Retrieved in'), false);
        assert.equal(await bodyHas(s.page, 'A plain answer.'), false);
        assert.equal((await buttonState(s.page, 'Ask')).disabled, false);
        await ctrlEnter(s.page);
        await waitText(s.page, 'A plain answer.');
        assert.equal(await bodyHas(s.page, 'Synthetic query failure.'), false);
        assert.equal(await bodyHas(s.page, 'Retrieved in'), true);
      } };
    })(),
    { name: 'End User sees no method display and the query asks for response_format end_user', open: qaOpen('end_user'), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select').length), 1);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select[disabled]').length), 0);
      await askQuestion(s.page);
      await waitText(s.page, 'A plain answer.');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.response_format, 'end_user');
    } },
    ...['engineer', 'developer'].map(role => ({ name: `${role === 'engineer' ? 'Engineer' : 'Developer'} sees the method display and the query asks for response_format engineer`, open: qaOpen(role), async run(s) {
      await waitSelected(s.page, 0);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select').length), 2);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('select[disabled]').length), 1);
      await askQuestion(s.page);
      await waitText(s.page, 'A plain answer.');
      assert.equal(callsTo(s, 'POST', '/api/query')[0].body.response_format, 'engineer');
    } })),
  ];
}

function retrievalCases() {
  const saved = over => ({ 'GET /api/retrieval/config/*': call => json(200, retrievalConfig(call.path.split('/').pop(), over)) });
  const open = routes => ({ role: 'engineer', path: '/retrieval', routes: base(routes) });
  const alphaLabel = page => page.evaluate(() => [...document.querySelectorAll('label')].find(l => l.textContent.includes('Meaning:'))?.textContent.trim() || null);
  return [
    { name: 'Retrieval page says the settings shown are the saved ones', open: open(saved({ retrieval_mode: 'semantic', top_k: 9, is_default: false })), async run(s) {
      await waitText(s.page, 'Showing the settings saved for this collection.');
      assert.equal(await bodyHas(s.page, 'showing defaults'), false);
      assert.equal(await s.page.evaluate(() => document.querySelector('input[name=mode][value=semantic]').checked), true);
      assert.ok(await bodyHas(s.page, 'Top-K Results: 9'));
    } },
    { name: 'Retrieval page says defaults are shown when nothing is saved', open: open(saved({ is_default: true })), async run(s) {
      await waitText(s.page, `No settings saved for this collection yet ${DASH} showing defaults.`);
      assert.equal(await bodyHas(s.page, 'Showing the settings saved'), false);
    } },
    { name: 'The alpha slider shows for Hybrid only, starts at the saved value, and the changed value is saved', open: open({ ...saved({ retrieval_mode: 'hybrid', alpha: 0.25, top_k: 7, is_default: false }),
        'POST /api/retrieval/config': call => json(200, { ...call.body, is_default: false }) }), async run(s) {
      await waitText(s.page, 'Showing the settings saved');
      assert.equal(await alphaLabel(s.page), `Keyword \u2190 Balance \u2192 Meaning: 0.25`);
      assert.equal(await s.page.evaluate(() => document.querySelectorAll('input[type=range]').length), 2);
      await s.page.click('input[name=mode][value=semantic]');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 1, 'the alpha slider to go (semantic)');
      assert.equal(await alphaLabel(s.page), null);
      await s.page.click('input[name=mode][value=hnsw]');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 1, 'no alpha slider (vector)');
      await s.page.click('input[name=mode][value=hybrid]');
      await waitFor(s.page, () => document.querySelectorAll('input[type=range]').length === 2, 'the alpha slider to return');
      await fillNth(s.page, 'input[type=range]', 1, 0.4);
      await waitFor(s.page, () => [...document.querySelectorAll('label')].some(l => l.textContent.trim().endsWith('Meaning: 0.4')), 'alpha 0.4');
      await press(s.page, 'Save for this collection');
      await waitText(s.page, 'Saved!');
      const posts = callsTo(s, 'POST', '/api/retrieval/config');
      assert.equal(posts.length, 1);
      assert.deepEqual(posts[0].body, { collection: A, retrieval_mode: 'hybrid', top_k: 7, alpha: 0.4, ef: null, response_format: 'engineer' });
    } },
    (() => {
      const h = hold();
      return { name: 'Save is disabled and the page says Loading while the saved settings are read, then it is enabled', open: open({ 'GET /api/retrieval/config/*': () => h }), async run(s) {
        await waitText(s.page, 'Loading saved settings' + ELLIPSIS);
        assert.equal((await buttonState(s.page, 'Save for this collection')).disabled, true);
        h.release(json(200, retrievalConfig(A)));
        await waitText(s.page, 'Loading saved settings' + ELLIPSIS, true);
        assert.equal((await buttonState(s.page, 'Save for this collection')).disabled, false);
      } };
    })(),
  ];
}

// The proxy answers some errors itself, as HTML; the client turns them into text
// (ui/src/api/client.ts). Driven through the Import page's upload.
function proxyCases() {
  const one = (name, answer, want) => ({ name, open: { role: 'engineer', path: '/import', routes: base({ 'POST /api/ingest/upload': answer }) }, async run(s) {
    await waitSelected(s.page, 0);
    await queueFiles(s.page, ['doc.txt']);
    await press(s.page, 'Start Ingest');
    await waitText(s.page, want);
    const shown = await s.page.evaluate(() => document.querySelector('p.text-red-600')?.textContent.trim());
    assert.equal(shown, want);
    assert.ok(!/JSON|Unexpected token|<html/i.test(shown), 'no parser text: ' + shown);
  } });
  return [
    one('A proxy 413 page is shown as the upload limit message', html(413, 'Request Entity Too Large'), 'The upload is larger than the 512 MB limit. Split it into smaller batches.'),
    one('A proxy 502 page is shown as "the API is not responding"', html(502, 'Bad Gateway'), 'The API is not responding. It may still be starting; try again in a minute.'),
    one('A proxy 504 page is shown as "the API took too long"', html(504, 'Gateway Time-out'), 'The API took too long to respond.'),
    one('An HTML error page with no special text is shown as its status', html(500, 'Internal Server Error'), 'HTTP 500'),
    one('A 200 that is not JSON is reported as unreadable', { status: 200, type: 'text/html', body: '<html><body>not json</body></html>' }, 'The server sent a response the UI could not read.'),
    one("The API's own JSON message wins over the status text", apiError(413, 'PAYLOAD_TOO_LARGE', 'Synthetic API message.'), 'Synthetic API message.'),
  ];
}

async function runUiControlTests(browser, baseUrl, reporter) {
  S.BASE = baseUrl;
  const run = (section, cases) => S.runCases(browser, baseUrl, reporter, section, cases);
  await run('Collections table and create modal (#400)', collectionsCases());
  await run('Import queue, sliders and "+ New" modal (#400)', importCases());
  await run('Navigation, roles and redirects; gating is client-side only until #25 (#400)', navigationCases());
  await run('Q&A input, citations toggle, answer display and End User view (#400)', qaCases());
  await run('Retrieval page status text, alpha slider and save (#400)', retrievalCases());
  await run('Client handling of the proxy error pages (#400)', proxyCases());
}

module.exports = { runUiControlTests };
if (require.main === module) (async () => {
  const { makeReporter } = require('./lib');
  const browser = await require('puppeteer-core').launch({ executablePath: process.env.RAG_CHROMIUM_PATH || '/usr/bin/chromium-browser', headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const reporter = makeReporter();
  try { await runUiControlTests(browser, process.env.RAG_UI_BASE || 'http://127.0.0.1:3000', reporter); }
  finally { await browser.close(); }
  process.exitCode = reporter.summary() ? 0 : 1;
})().catch(e => { console.error(e); process.exitCode = 2; });
