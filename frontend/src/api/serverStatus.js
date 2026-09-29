import { API_BASE } from './client';

// The free-tier backend sleeps when idle, and waking it (Render, then Neon) can
// take up to a minute. We ping it once on page load so it is usually awake by
// the time someone submits a form, and let pages show a banner meanwhile.
//
// status: 'checking' → first ping in flight, not yet slow enough to mention
//         'waking'   → slow or failing; the server is starting up
//         'ready'    → answered
//         'down'     → still failing after MAX_WAIT_MS

const SLOW_AFTER_MS = 1500;
const RETRY_EVERY_MS = 3000;
const MAX_WAIT_MS = 120000;

let status = 'checking';
let started = false;
const listeners = new Set();

function setStatus(next) {
  if (next === status) return;
  status = next;
  listeners.forEach((fn) => fn(status));
}

async function ping() {
  try {
    const res = await fetch(`${API_BASE}/health/ready`, { cache: 'no-store' });
    return res.ok;
  } catch {
    return false;
  }
}

export function wakeServer() {
  if (started) return;
  started = true;

  const deadline = Date.now() + MAX_WAIT_MS;
  const slowTimer = setTimeout(() => {
    if (status === 'checking') setStatus('waking');
  }, SLOW_AFTER_MS);

  (async () => {
    while (Date.now() < deadline) {
      if (await ping()) {
        clearTimeout(slowTimer);
        setStatus('ready');
        return;
      }
      setStatus('waking');
      await new Promise((r) => setTimeout(r, RETRY_EVERY_MS));
    }
    clearTimeout(slowTimer);
    setStatus('down');
  })();
}

export function getServerStatus() {
  return status;
}

export function subscribeServerStatus(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
