// Shared fixture for the UI control checks (#400): the real built UI in a fresh
// browser context, with every /api/ request answered by the check itself. Nothing
// reaches the backend: an /api/ request with no stub is answered 599 and recorded,
// and the case fails on it.
const { makeReporter } = require('./lib');

const json = (status, body) => ({ status, type: 'application/json', body: JSON.stringify(body) });
const apiError = (status, code, message) => json(status, { error: { code, message, detail: null } });
// nginx's own error page, which the proxy sends when it answers instead of the API.
const html = (status, title) => ({ status, type: 'text/html', body: `<html><head><title>${status} ${title}</title></head><body><center><h1>${status} ${title}</h1></center><hr><center>nginx</center></body></html>` });

// A response the case releases by hand: `route: () => h.promise`, later `h.release(answer)`.
function hold() {
  let release;
  const promise = new Promise(r => { release = r; });
  return { promise, release };
}

// routes: { 'METHOD /api/path': answer | (call) => answer | Promise<answer> }.
// A key ending in '*' matches any path with that prefix. `call` is
// { method, path, query, body (parsed JSON, or the raw text), raw }.
async function openPage(browser, base, { role = 'engineer', routes = {}, path = '/', fastPolls = false, onDialog } = {}) {
  const ctx = await browser.createBrowserContext();
  const page = await ctx.newPage();
  const errors = [], unstubbed = [], calls = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('dialog', async d => { if (onDialog) await onDialog(d); else await d.dismiss(); });
  await page.evaluateOnNewDocument((r, fast) => {
    // FormData bodies, which the request interception cannot read back reliably.
    window.__forms = [];
    const realFetch = window.fetch.bind(window);
    window.fetch = (input, init) => {
      if (init && init.body instanceof FormData) window.__forms.push([...init.body.entries()].map(([k, v]) => [k, typeof v === 'string' ? v : 'file:' + v.name]));
      return realFetch(input, init);
    };
    // Seeded once: a case that signs out must stay signed out on the next load.
    if (r && !sessionStorage.getItem('vfy_seeded')) { sessionStorage.setItem('vfy_seeded', '1'); sessionStorage.setItem('rag_role', JSON.stringify({ role: r })); }
    if (fast) {
      const si = window.setInterval.bind(window), st = window.setTimeout.bind(window);
      window.setInterval = (fn, ms, ...a) => si(fn, ms === 2000 ? 20 : ms, ...a);
      window.setTimeout = (fn, ms, ...a) => st(fn, ms === 2000 ? 20 : ms, ...a);
    }
  }, role, fastPolls);
  await page.setRequestInterception(true);
  page.on('request', async request => {
    const url = new URL(request.url());
    if (!url.pathname.startsWith('/api/')) return request.continue();
    const method = request.method();
    const raw = request.postData() || '';
    let body = raw;
    try { body = raw ? JSON.parse(raw) : null; } catch { /* multipart or text: kept raw */ }
    const call = { method, path: url.pathname, query: url.search, body, raw };
    calls.push(call);
    let route = routes[method + ' ' + url.pathname];
    if (route === undefined) {
      const key = Object.keys(routes).find(k => k.endsWith('*') && (method + ' ' + url.pathname).startsWith(k.slice(0, -1)));
      route = key === undefined ? undefined : routes[key];
    }
    if (route === undefined) { unstubbed.push(method + ' ' + url.pathname); return request.respond({ status: 599, contentType: 'text/plain', body: 'unstubbed' }); }
    const answer = typeof route === 'function' ? await route(call) : route;
    const a = answer && answer.promise ? await answer.promise : answer;
    try { await request.respond({ status: a.status, contentType: a.type || 'application/json', body: a.body }); }
    catch { /* the page navigated away while the answer was held */ }
  });
  if (path) await page.goto(base + path, { waitUntil: 'domcontentloaded' });
  return { ctx, page, errors, unstubbed, calls };
}

const callsTo = (s, method, path) => s.calls.filter(c => c.method === method && c.path === path);
const text = page => page.evaluate(() => document.body.innerText.replace(/\s+/g, ' ').trim());

async function waitFor(page, fn, why, ...args) {
  try { await page.waitForFunction(fn, { timeout: 8000 }, ...args); }
  catch { throw new Error(`timed out waiting for ${why}; page: ${(await text(page)).slice(0, 500)}`); }
}
const waitText = (page, want, gone = false) =>
  waitFor(page, (w, g) => document.body.innerText.replace(/\s+/g, ' ').includes(w) !== g, `"${want}" to ${gone ? 'go' : 'appear'}`, want, gone);

// Buttons or links by exact trimmed text.
const click = (page, label, selector = 'button') => page.evaluate((t, sel) => {
  const el = [...document.querySelectorAll(sel)].find(x => x.textContent.trim() === t);
  if (!el) return false; el.click(); return true;
}, label, selector);
async function press(page, label, selector = 'button') {
  if (!await click(page, label, selector)) throw new Error(`no ${selector} "${label}"; page: ${(await text(page)).slice(0, 300)}`);
}

// React ignores a plain .value assignment; use the native setter and the right event.
const fill = (page, selector, value) => page.evaluate((sel, v) => {
  const el = document.querySelector(sel);
  if (!el) throw new Error('no element ' + sel);
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, String(v));
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
}, selector, value);

// Sets the n-th element matching a selector, for sliders that share a selector.
const fillNth = (page, selector, n, value) => page.evaluate((sel, i, v) => {
  const el = document.querySelectorAll(sel)[i];
  if (!el) throw new Error(`no element ${sel}[${i}]`);
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, String(v));
  el.dispatchEvent(new Event(el instanceof HTMLSelectElement ? 'change' : 'input', { bubbles: true }));
}, selector, n, value);

const forms = page => page.evaluate(() => window.__forms);
const buttonState = (page, label) => page.evaluate(t => {
  const b = [...document.querySelectorAll('button')].find(x => x.textContent.trim() === t);
  return b ? { disabled: b.disabled } : null;
}, label);

const nav = page => page.evaluate(() => [...document.querySelectorAll('nav a')].map(a => a.textContent.trim()));
const path = page => page.evaluate(() => location.pathname);
async function waitPath(page, want) {
  await waitFor(page, w => location.pathname === w, `the path ${want}`, want);
}

// Runs each { name, opts, run(session) } in its own page. A case fails on a thrown
// error, a React page error or an /api/ request it did not stub.
async function runCases(browser, base, reporter, section, cases) {
  reporter.section(section);
  for (const c of cases) {
    let s;
    try {
      s = await openPage(browser, base, c.open || {});
      await c.run(s);
      if (s.errors.length) throw new Error('page errors: ' + s.errors.join(' | '));
      if (s.unstubbed.length) throw new Error('unstubbed API calls: ' + s.unstubbed.join(', '));
      reporter.check(c.name, true);
    } catch (e) {
      reporter.check(c.name, false, String(e.message || e).slice(0, 700));
    } finally {
      if (s) await s.ctx.close().catch(() => {});
    }
  }
}

module.exports = { json, apiError, html, hold, openPage, callsTo, text, waitFor, waitText, click, press, fill, fillNth, forms, buttonState, nav, path, waitPath, runCases, makeReporter };
