import { Mark } from "./Mark";
import { StatusDot } from "./primitives";
import { summarise } from "../lib/engine";
import { useStore } from "../store/useStore";

export function Topbar({
  onAccount, onEngine,
}: { onAccount: () => void; onEngine: () => void }) {
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
