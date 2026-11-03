'use strict';
// Local-only HTTP bridge. No dependency on the `vscode` module so it can be unit tested.
const http = require('http');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');

const MAX_BODY = 256 * 1024;
const MAX_FIELD = 60000;
const MAX_ANSWER = 8000;

class BridgeError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

function defaultInfoPath() {
  if (process.env.APP_BUILDER_BRIDGE_INFO) return process.env.APP_BUILDER_BRIDGE_INFO;
  return path.join(require('os').homedir(), '.app-builder', 'vscode-bridge.json');
}

function safeEqual(a, b) {
  const x = Buffer.from(String(a)); const y = Buffer.from(String(b));
  return x.length === y.length && crypto.timingSafeEqual(x, y);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = []; let size = 0; let tooBig = false;
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) { tooBig = true; chunks.length = 0; return; }   // keep draining so the 413 can be delivered
      chunks.push(c);
    });
    req.on('end', () => (tooBig ? reject(new BridgeError(413, 'request too large')) : resolve(Buffer.concat(chunks).toString('utf8'))));
    req.on('error', reject);
  });
}

function createBridge({ ask, infoPath = defaultInfoPath(), version = '0.1.0', log = () => {}, timeoutMs = 90000 }) {
  const token = crypto.randomBytes(32).toString('hex');
  let server = null; let port = 0; let tail = Promise.resolve();

  function send(res, status, body) {
    const data = JSON.stringify(body);
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': Buffer.byteLength(data), 'Cache-Control': 'no-store' });
    res.end(data);
  }

  function authorised(req) {
    const host = String(req.headers.host || '');
    if (host !== `127.0.0.1:${port}` && host !== `localhost:${port}`) return false;   // DNS rebinding
    if (req.headers.origin) return false;                                              // browsers never call this
    const m = /^Bearer (.+)$/.exec(String(req.headers.authorization || ''));
    return !!m && safeEqual(m[1], token);
  }

  // One question at a time: Copilot asks for consent and rate-limits, so queue instead of racing.
  function serialised(job) {
    const run = tail.then(job, job);
    tail = run.catch(() => {});
    return run;
  }

  async function handle(req, res) {
    try {
      if (!authorised(req)) return send(res, 401, { error: 'unauthorised' });
      const url = String(req.url || '').split('?')[0];
      if (req.method === 'GET' && url === '/health') return send(res, 200, { ok: true, version });
      if (req.method === 'POST' && url === '/ask') {
        let data;
        try { data = JSON.parse(await readBody(req)); } catch (e) {
          if (e instanceof BridgeError) throw e;
          throw new BridgeError(400, 'body must be JSON');
        }
        const system = data && data.system === undefined ? '' : data && data.system;
        const user = data && data.user;
        if (typeof system !== 'string' || typeof user !== 'string' || !user.trim() ||
            system.length > MAX_FIELD || user.length > MAX_FIELD) {
          throw new BridgeError(400, 'system/user must be strings (user not empty, each under 60000 chars)');
        }
        const text = await serialised(() => Promise.race([
          Promise.resolve(ask({ system, user })),
          new Promise((_, rej) => setTimeout(() => rej(new BridgeError(504, 'model timed out')), timeoutMs).unref()),
        ]));
        return send(res, 200, { text: String(text || '').slice(0, MAX_ANSWER) });
      }
      return send(res, 404, { error: 'not found' });
    } catch (err) {
      const status = err instanceof BridgeError ? err.status : 500;
      if (status === 500) log('bridge error: ' + (err && err.message));
      return send(res, status, { error: status === 500 ? 'internal error' : err.message });
    }
  }

  function writeInfo() {
    fs.mkdirSync(path.dirname(infoPath), { recursive: true, mode: 0o700 });
    const tmp = infoPath + '.' + process.pid + '.tmp';
    fs.writeFileSync(tmp, JSON.stringify({ port, token, pid: process.pid, version, started: new Date().toISOString() }), { mode: 0o600 });
    fs.renameSync(tmp, infoPath);
  }

  function removeInfo() {
    try {
      const cur = JSON.parse(fs.readFileSync(infoPath, 'utf8'));
      if (cur.token === token) fs.unlinkSync(infoPath);     // never delete another window's file
    } catch (_) { /* already gone */ }
  }

  return {
    get port() { return port; },
    get token() { return token; },
    start() {
      return new Promise((resolve, reject) => {
        server = http.createServer(handle);
        server.on('error', reject);
        server.listen(0, '127.0.0.1', () => {
          port = server.address().port; writeInfo();
          process.once('exit', removeInfo);          // VS Code may stop the extension host without calling deactivate
          log('listening on 127.0.0.1:' + port); resolve(port);
        });
      });
    },
    stop() {
      return new Promise((resolve) => { process.removeListener('exit', removeInfo); removeInfo(); if (!server) return resolve(); server.close(() => resolve()); server.closeAllConnections && server.closeAllConnections(); });
    },
  };
}

module.exports = { createBridge, BridgeError };
