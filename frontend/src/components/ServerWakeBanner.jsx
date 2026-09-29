import { useEffect, useState } from 'react';
import useServerStatus from '../hooks/useServerStatus';
import './ServerWakeBanner.css';

// Explains the cold-start pause on free hosting instead of letting the page
// look frozen. Silent when the server answers quickly.
export default function ServerWakeBanner() {
  const status = useServerStatus();
  const [wasWaking, setWasWaking] = useState(false);
  const [showReady, setShowReady] = useState(false);

  useEffect(() => {
    if (status === 'waking') setWasWaking(true);
    if (status === 'ready' && wasWaking) {
      setShowReady(true);
      const t = setTimeout(() => setShowReady(false), 2500);
      return () => clearTimeout(t);
    }
  }, [status, wasWaking]);

  if (status === 'waking') {
    return (
      <div className="wake-banner" role="status">
        <div className="wake-spinner" />
        <div>
          <div className="wake-title">Waking up the server…</div>
          <div className="wake-text">
            This demo runs on free hosting that sleeps when idle. The first load
            can take up to a minute. You can fill in the form meanwhile.
          </div>
        </div>
      </div>
    );
  }
  if (status === 'down') {
    return (
      <div className="wake-banner wake-banner--error" role="alert">
        <div>
          <div className="wake-title">Server is not responding</div>
          <div className="wake-text">Please refresh the page in a minute.</div>
        </div>
      </div>
    );
  }
  if (showReady) {
    return (
      <div className="wake-banner wake-banner--ready" role="status">
        <div className="wake-title">✓ Server is ready</div>
      </div>
    );
  }
  return null;
}
