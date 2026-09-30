import { useStore } from "../store/useStore";
import { summarise } from "../lib/engine";
import { Spinner, StatusDot, type Tone } from "./primitives";

function StateOrb({ tone }: { tone: Tone }) {
  return <span className={`status-orb status-orb-${tone}`} aria-hidden="true" />;
}

/* This is the shell's instrument rail: four terse answers in flexible blocks.
   The detailed engine explanation remains in the topbar sheet. */

export function ActiveRoute() {
  const snapshot = useStore((s) => s.snapshot);
  const busy = useStore((s) => s.busyModel);
  const unload = useStore((s) => s.unload);

  const active = snapshot?.models.find((model) => model.active);
  const engine = summarise(snapshot?.engine, snapshot?.runtime);
  const unloading = busy === "__unload__";
  const owned = Boolean(snapshot?.engine.owned);
  const gateway = !snapshot
    ? "—"
    : snapshot.engine.owned
      ? "Owned"
      : snapshot.engine.reachable
        ? "External"
        : "Idle";
  const gatewayTone: Tone = !snapshot
    ? "cold"
    : snapshot.engine.owned
      ? "live"
      : snapshot.engine.reachable
        ? "warm"
        : "cold";
  const laneBusy = Boolean(snapshot?.locked || busy);

  return (
    <section className="hero status-rail" aria-label="Server status">
      <article className="status-card status-card-current">
        <div className="status-copy">
          <span className="eyebrow">Current status</span>
          <strong className="status-value" title={active?.id}>
            <StatusDot tone={engine.tone} pulse={engine.pulse} />
            <span>{active ? active.name || active.id : "Nothing loaded"}</span>
          </strong>
        </div>
        <button
          className="status-action"
          type="button"
          disabled={!owned || unloading}
          aria-label={unloading ? "Unloading active model" : "Unload active model"}
          title={owned ? "Unload active model" : "No ENGRAI-owned model is loaded"}
          onClick={() => { void unload(); }}
        >
          {unloading ? (
            <Spinner />
          ) : (
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M12 3v9m6.4-6.4a9 9 0 1 1-12.8 0" />
            </svg>
          )}
        </button>
      </article>

      <article className="status-card">
        <StateOrb tone={gatewayTone} />
        <div className="status-copy">
          <span className="eyebrow">Gateway</span>
          <strong className="status-value">{gateway}</strong>
        </div>
      </article>

      <article className="status-card">
        <StateOrb tone={engine.tone} />
        <div className="status-copy">
          <span className="eyebrow">Engine</span>
          <strong className="status-value">
            {snapshot?.engine.version ? `v${snapshot.engine.version.replace(/^v/i, "")}` : engine.label}
          </strong>
        </div>
      </article>

      <article className="status-card">
        <StateOrb tone={laneBusy ? "warm" : "live"} />
        <div className="status-copy">
          <span className="eyebrow">Lane</span>
          <strong className="status-value">{laneBusy ? "Busy" : "Open"}</strong>
        </div>
      </article>
    </section>
  );
}
