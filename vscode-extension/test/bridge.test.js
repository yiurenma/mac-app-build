'use strict';
const test = require('node:test');
const assert = require('node:assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const http = require('http');
const { createBridge, BridgeError } = require('../lib/bridge');

function call(b, { method = 'POST', url = '/ask', body, headers = {}, auth = true, host } = {}) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body));
    const h = { Host: host || `127.0.0.1:${b.port}`, ...headers };
    if (auth) h.Authorization = 'Bearer ' + b.token;
    if (data !== undefined) { h['Content-Type'] = 'application/json'; h['Content-Length'] = Buffer.byteLength(data); }
    const req = http.request({ host: '127.0.0.1', port: b.port, method, path: url, headers: h }, (res) => {
      let s = ''; res.on('data', (c) => (s += c)); res.on('end', () => resolve({ status: res.statusCode, json: s ? JSON.parse(s) : null }));
    });
    req.on('error', reject);
    if (data !== undefined) req.write(data);
    req.end();
  });
}

async function withBridge(ask, fn, opts = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-'));
  const infoPath = path.join(dir, 'info.json');
  const b = createBridge({ ask, infoPath, ...opts });
  await b.start();
  try { await fn(b, infoPath); } finally { await b.stop(); fs.rmSync(dir, { recursive: true, force: true }); }
}

test('writes a private info file and removes it on stop', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-'));
  const infoPath = path.join(dir, 'sub', 'info.json');
  const b = createBridge({ ask: async () => 'x', infoPath });
  await b.start();
  const info = JSON.parse(fs.readFileSync(infoPath, 'utf8'));
  assert.strictEqual(info.port, b.port); assert.strictEqual(info.token, b.token); assert.strictEqual(info.pid, process.pid);
  if (process.platform !== 'win32') assert.strictEqual(fs.statSync(infoPath).mode & 0o777, 0o600);
  await b.stop();
  assert.ok(!fs.existsSync(infoPath));
});

test('does not delete an info file written by another window', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bridge-'));
  const infoPath = path.join(dir, 'info.json');
  const b = createBridge({ ask: async () => 'x', infoPath });
  await b.start();
  fs.writeFileSync(infoPath, JSON.stringify({ port: 1, token: 'other-window' }));
  await b.stop();
  assert.ok(fs.existsSync(infoPath));
});

test('answers a question', () => withBridge(async ({ system, user }) => `S=${system}|U=${user}`, async (b) => {
  const r = await call(b, { body: { system: 'sys', user: 'hi' } });
  assert.deepStrictEqual([r.status, r.json.text], [200, 'S=sys|U=hi']);
  assert.strictEqual((await call(b, { method: 'GET', url: '/health' })).json.ok, true);
}));

test('rejects missing or wrong token, foreign Host and any Origin', () => withBridge(async () => 'x', async (b) => {
  assert.strictEqual((await call(b, { body: { user: 'a' }, auth: false })).status, 401);
  assert.strictEqual((await call(b, { body: { user: 'a' }, auth: false, headers: { Authorization: 'Bearer nope' } })).status, 401);
  assert.strictEqual((await call(b, { body: { user: 'a' }, host: 'evil.example:' + b.port })).status, 401);
  assert.strictEqual((await call(b, { body: { user: 'a' }, headers: { Origin: 'http://evil.example' } })).status, 401);
}));

test('validates input', () => withBridge(async () => 'x', async (b) => {
  for (const body of ['not json', { user: '' }, { user: 5 }, { system: 3, user: 'a' }, { user: 'x'.repeat(60001) }]) {
    assert.strictEqual((await call(b, { body })).status, 400, JSON.stringify(body).slice(0, 40));
  }
  assert.strictEqual((await call(b, { body: 'x'.repeat(300 * 1024) })).status, 413);
  assert.strictEqual((await call(b, { method: 'GET', url: '/nope' })).status, 404);
}));

test('maps model errors and hides internals', () => {
  let n = 0;
  const ask = async () => { n += 1; if (n === 1) throw new BridgeError(503, 'no model'); throw new Error('secret stack detail'); };
  return withBridge(ask, async (b) => {
    const a = await call(b, { body: { user: 'a' } });
    assert.deepStrictEqual([a.status, a.json.error], [503, 'no model']);
    const c = await call(b, { body: { user: 'a' } });
    assert.deepStrictEqual([c.status, c.json.error], [500, 'internal error']);
  });
});

test('times out slow models', () => withBridge(() => new Promise(() => {}), async (b) => {
  assert.strictEqual((await call(b, { body: { user: 'a' } })).status, 504);
}, { timeoutMs: 100 }));

test('serialises concurrent questions', () => {
  let active = 0; let peak = 0;
  const ask = async ({ user }) => { active += 1; peak = Math.max(peak, active); await new Promise((r) => setTimeout(r, 40)); active -= 1; return user; };
  return withBridge(ask, async (b) => {
    const rs = await Promise.all(['a', 'b', 'c'].map((u) => call(b, { body: { user: u } })));
    assert.deepStrictEqual(rs.map((r) => r.json.text), ['a', 'b', 'c']);
    assert.strictEqual(peak, 1);
  });
});

test('truncates very long answers', () => withBridge(async () => 'y'.repeat(20000), async (b) => {
  assert.strictEqual((await call(b, { body: { user: 'a' } })).json.text.length, 8000);
}));
