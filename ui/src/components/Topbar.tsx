import { Mark } from "./Mark";
import { StatusDot } from "./primitives";
import { summarise } from "../lib/engine";
import { useStore } from "../store/useStore";

export function Topbar({
  onAccount, onEngine, onLedger,
}: { onAccount: () => void; onEngine: () => void; onLedger: () => void }) {
  const snapshot = useStore((s) => s.snapshot);
  const profile = useStore((s) => s.profile);
  const offline = useStore((s) => s.offline);
  const engine = summarise(snapshot?.engine, snapshot?.runtime);
  const engineLabel = offline
    ? "Gateway unreachable · Engine unavailable"
    : `Engine ${engine.label === "Live" ? "Live" : engine.label}`;

  return (
    <header className="topbar">
      <a className="brand" href="/" aria-label="ENGRAI SERVER">
        <Mark size={30} />
        <span className="brand-copy">
          <strong>ENGRAI SERVER</strong>
        </span></a>

      <div className="topbar-actions">
        {offline && <span className="offline-chip">Gateway Unreachable</span>}
        {/* Same tab on purpose: the session lives in this tab's storage. */}
        <a className="studio-link" href="/cardgen/" title="Card Studio: author character cards with the loaded models">
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="5" y="3" width="14" height="18" rx="2" />
            <path d="M9 8h6M9 12h6M9 16h3" />
          </svg>
          <span>Card Studio</span>
        </a>
        <button
          className="studio-link"
          type="button"
          onClick={onLedger}
          data-testid="open-ledger"
          title="Ledger: what the models know about you"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z" />
            <path d="M5 17a3 3 0 0 1 3-3h11M9 8h6" />
          </svg>
          <span>Ledger</span>
        </button>
        <button className="engine-pill" type="button" onClick={onEngine} aria-label={engineLabel} title={engineLabel}>
          <StatusDot tone={offline ? "bad" : engine.tone} pulse={!offline && engine.pulse} />
          <span>{offline ? "Engine unavailable" : `Engine ${engine.label === "Live" ? "Live" : engine.label}`}</span>
        </button>
        <button
          className="avatar"
          type="button"
          onClick={onAccount}
          aria-label="Profile and access"
        >
          {profile?.initials || "··"}
        </button>
      </div>
    </header>
  );
}
