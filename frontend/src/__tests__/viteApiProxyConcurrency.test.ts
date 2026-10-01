// @vitest-environment node
/**
 * MP-4C follow-up - the Vite dev-server `/api` proxy must not let ONE open SSE
 * stream starve every other API request.
 *
 * This is a REAL reproduction against the SHIPPED proxy options: it starts the
 * project OWN vite dev server (loading frontend/vite.config.ts, so the options
 * under test are the shipped ones) and points it at a stub backend that holds
 * `/api/stream` open - the same shape as a durable run parked at
 * `waiting_for_approval`.
 *
 * RED before the fix: with `maxSockets: 1` the parked stream owns the only
 * socket, so the approval detail, the exact-code preview and health never
 * complete through the proxy within the bounded timeout.
 */
import http from 'http';
import path from 'path';
import { fileURLToPath } from 'url';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { createServer, type ViteDevServer } from 'vite';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = path.resolve(HERE, '..', '..');

/** Bounded timeout: a request that needs longer than this has been starved. */
const REQUEST_TIMEOUT_MS = 3000;

let backend: http.Server;
let backendPort = 0;
let vite: ViteDevServer;
let vitePort = 0;
const heldResponses = new Set<http.IncomingMessage>();

function sendJson(res: http.ServerResponse, code: number, body: unknown): void {
  const text = JSON.stringify(body);
  res.writeHead(code, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(text) });
  res.end(text);
}

async function freePort(): Promise<number> {
  return await new Promise<number>((resolve, reject) => {
    const probe = http.createServer();
    probe.on('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const port = (probe.address() as { port: number }).port;
      probe.close(() => resolve(port));
    });
  });
}

type Opened = { status: number; close: () => void };

/** Start a request and resolve as soon as the response HEADERS arrive. */
function openStream(port: number, pathname: string): Promise<Opened> {
  return new Promise<Opened>((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path: pathname, method: 'GET' }, (res) => {
      heldResponses.add(res);
      resolve({
        status: res.statusCode || 0,
        close: () => {
          heldResponses.delete(res);
          req.destroy();
        },
      });
    });
    req.on('error', reject);
    req.end();
  });
}

/** A bounded request that FAILS LOUDLY when the proxy starves it. */
function get(port: number, pathname: string, timeoutMs = REQUEST_TIMEOUT_MS): Promise<number> {
  return new Promise<number>((resolve, reject) => {
    let settled = false;
    const req = http.request({ host: '127.0.0.1', port, path: pathname, method: 'GET' }, (res) => {
      settled = true;
      res.resume();
      resolve(res.statusCode || 0);
    });
    req.setTimeout(timeoutMs, () => {
      if (settled) return;
      req.destroy();
      reject(new Error('starved: GET ' + pathname + ' did not complete within ' + timeoutMs + 'ms'));
    });
    req.on('error', (err) => { if (!settled) reject(err); });
    req.end();
  });
}

beforeAll(async () => {
  backend = http.createServer((req, res) => {
    const url = req.url || '/';
    if (url === '/api/stream') {
      res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
      res.write(': connected' + String.fromCharCode(10) + String.fromCharCode(10));
      return;                       // held open deliberately
    }
    if (url === '/api/health') return sendJson(res, 200, { status: 'ok' });
    if (url === '/api/approvals/x') return sendJson(res, 200, { id: 'x', state: 'requested' });
    if (url === '/api/approvals/x/action') return sendJson(res, 200, { code: 'print(2 + 2)' });
    sendJson(res, 404, { detail: 'not found' });
  });
  await new Promise<void>((resolve) => backend.listen(0, '127.0.0.1', () => resolve()));
  backendPort = (backend.address() as { port: number }).port;

  // The SHIPPED dev proxy reads its target from this NODE-side variable.
  process.env.THUNDER_DEV_PROXY_TARGET = 'http://127.0.0.1:' + backendPort;
  vitePort = await freePort();
  vite = await createServer({
    root: FRONTEND_ROOT,
    logLevel: 'silent',
    server: { port: vitePort, strictPort: true },
  });
  await vite.listen();
});

afterAll(async () => {
  for (const res of heldResponses) { try { res.destroy(); } catch { /* ignore */ } }
  heldResponses.clear();
  if (vite) await vite.close();
  if (backend) await new Promise<void>((resolve) => backend.close(() => resolve()));
  delete process.env.THUNDER_DEV_PROXY_TARGET;
});

// ── the control: the backend itself is fine ──────────────────────────────
describe('stub backend (control)', () => {
  it('serves approval detail, exact-code preview and health while a stream is open', async () => {
    const stream = await openStream(backendPort, '/api/stream');
    expect(stream.status).toBe(200);
    const codes = await Promise.all([
      get(backendPort, '/api/health'),
      get(backendPort, '/api/approvals/x'),
      get(backendPort, '/api/approvals/x/action'),
    ]);
    expect(codes).toEqual([200, 200, 200]);
    stream.close();
  });
});

// ── the defect: the REAL Vite /api proxy ─────────────────────────────────
describe('vite /api proxy concurrency', () => {
  it('does not let one parked SSE stream starve other API requests', async () => {
    const stream = await openStream(vitePort, '/api/stream');
    expect(stream.status).toBe(200);
    let detail = 0;
    let preview = 0;
    let health = 0;
    try {
      [health, detail, preview] = await Promise.all([
        get(vitePort, '/api/health'),
        get(vitePort, '/api/approvals/x'),
        get(vitePort, '/api/approvals/x/action'),
      ]);
    } finally {
      stream.close();
    }
    expect({ health, detail, preview }).toEqual({ health: 200, detail: 200, preview: 200 });
  });

  it('keeps serving other API requests with THREE simultaneous streams open', async () => {
    const streams = await Promise.all([
      openStream(vitePort, '/api/stream'),
      openStream(vitePort, '/api/stream'),
      openStream(vitePort, '/api/stream'),
    ]);
    try {
      const codes = await Promise.all([
        get(vitePort, '/api/health'),
        get(vitePort, '/api/approvals/x'),
        get(vitePort, '/api/approvals/x/action'),
      ]);
      expect(codes).toEqual([200, 200, 200]);
    } finally {
      streams.forEach((s) => s.close());
    }
  });
});
