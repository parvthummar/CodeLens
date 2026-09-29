import { API_BASE } from './config';

// The free-tier backend sleeps when idle, and waking it (Render, then Neon) can
// take up to a minute. We ping it on page load so it is usually awake by the
// time someone submits a form, and let pages show a banner meanwhile.
//
// status: 'checking' → first ping in flight, not yet slow enough to mention
//         'waking'   → slow or failing; the server is starting up
//         'ready'    → answered (a ping, or any other API call)
//         'down'     → still failing after GIVE_UP_NOTICE_MS; pings continue

// A fresh TLS connection plus a Neon compute resuming can take a couple of
// seconds even with Render awake; only a real cold start should show a banner.
const SLOW_AFTER_MS = 4000;
// Render holds a request open while the instance boots, and that request can
// hang long past the moment the app is up. Abandon it and ask again.
const PING_TIMEOUT_MS = 8000;
const RETRY_EVERY_MS = 3000;
const GIVE_UP_NOTICE_MS = 120000;

let status = 'checking';
let started = false;
const listeners = new Set();

function setStatus(next) {
  if (next === status) return;
  status = next;
  listeners.forEach((fn) => fn(status));
}

async function ping() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), PING_TIMEOUT_MS);
  try {
    // Not /health/...: ad blockers match that path and block it client-side.
    const res = await fetch(`${API_BASE}/api/v1/ready`, {
      cache: 'no-store',
      signal: controller.signal,
    });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

export function markServerReachable() {
  setStatus('ready');
}

export function wakeServer() {
  if (started) return;
  started = true;

  const startedAt = Date.now();
  setTimeout(() => {
    if (status === 'checking') setStatus('waking');
  }, SLOW_AFTER_MS);

  (async () => {
    // Keep going until something succeeds; 'down' is a notice, not a stop.
    while (status !== 'ready') {
      if (await ping()) {
        setStatus('ready');
        return;
      }
      if (status === 'ready') return;
      setStatus(Date.now() - startedAt > GIVE_UP_NOTICE_MS ? 'down' : 'waking');
      await new Promise((r) => setTimeout(r, RETRY_EVERY_MS));
    }
  })();
}

export function getServerStatus() {
  return status;
}

export function subscribeServerStatus(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
