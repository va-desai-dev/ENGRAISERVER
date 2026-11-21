import { useStore } from "../store/useStore";
import { summarise } from "../lib/engine";
import { Spinner, StatusDot, type Tone } from "./primitives";

type TelemetryKind = "gateway" | "engine" | "lane";

function TelemetryBadge({
  kind,
  tone,
  active = false,
}: {
  kind: TelemetryKind;
  tone: Tone;
  active?: boolean;
}) {
  return (
    <span
      className={`telemetry-badge telemetry-badge-${tone}${active ? " is-active" : ""}`}
      aria-hidden="true"
    >
      {kind === "engine" && active && (
        <svg className="telemetry-reticle" viewBox="0 0 40 40">
          <circle
            cx="20"
            cy="20"
            r="18"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.2"
            strokeDasharray="3 5"
          />
        </svg>
      )}

      {kind === "gateway" && (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
          {tone === "live" ? (
            <path d="m9 12 2 2 4-4" />
          ) : (
            <circle cx="12" cy="11" r="1.5" />
          )}
        </svg>
      )}

      {kind === "engine" && (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <rect x="4" y="4" width="16" height="16" rx="2" />
          <rect x="9" y="9" width="6" height="6" />
          <path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 14h3M1 9h3M1 14h3" />
        </svg>
      )}

      {kind === "lane" && (
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <line x1="6" y1="3" x2="6" y2="15" />
          <circle cx="18" cy="6" r="3" />
          <circle cx="6" cy="18" r="3" />
          <path d="M18 9a9 9 0 0 1-9 9" />
        </svg>
      )}
    </span>
  );
}

/* Model status sits above the shell's three telemetry cards.
   The detailed engine explanation remains in the topbar sheet. */

export function ActiveRoute() {
  const snapshot = useStore((s) => s.snapshot);
  const busy = useStore((s) => s.busyModel);
  const busyImage = useStore((s) => s.busyImage);
  const unload = useStore((s) => s.unload);
  const unloadImage = useStore((s) => s.unloadImage);

  const active = snapshot?.models.find((model) => model.active);
  const activeImage = snapshot?.image_models.find((model) => model.active);
  const engine = summarise(snapshot?.engine, snapshot?.runtime);
  const imageEngine = summarise(snapshot?.image_engine, snapshot?.image_runtime);
  const engineLoading = Boolean(
    (busy && busy !== "__unload__") ||
    snapshot?.background?.kind === "load" ||
    snapshot?.runtime.phase === "starting" ||
    snapshot?.runtime.phase === "switching",
  );
  const engineName = snapshot?.engine.version || (engineLoading ? "Starting" : engine.label);
  const engineLabel = engineLoading
    ? `Revving ${engineName}`
    : engine.label === "Idle" ? "Engine Idle" : engineName;
  const owned = Boolean(snapshot?.engine.owned);
  const gateway = !snapshot
    ? "—"
    : snapshot.engine.owned
      ? "Live Gateway"
      : snapshot.engine.reachable
        ? "External Gateway"
        : "Idle Gateway";
  const gatewayTone: Tone = !snapshot
    ? "cold"
    : snapshot.engine.owned
      ? "live"
      : snapshot.engine.reachable
        ? "warm"
        : "cold";
  // Activity takes priority: a load can be busy before a model is active.
  const laneBusy = Boolean(
    snapshot?.locked || snapshot?.image_locked || snapshot?.background || busy || busyImage,
  );
  const laneLoaded = Boolean(active || activeImage);
  const laneTone: Tone = laneBusy ? "warm" : laneLoaded ? "live" : "cold";
  const laneLabel = laneBusy ? "Queued" : laneLoaded ? "Available" : "Idle";
  const laneColor = laneBusy ? "var(--warn)" : laneLoaded ? "var(--accent)" : "var(--muted-foreground)";
  const modelStatuses = [
    { kind: "Text", model: active, engine, owned, busy, unload },
    {
      kind: "Image",
      model: activeImage,
      engine: imageEngine,
      owned: Boolean(snapshot?.image_engine.owned),
      busy: busyImage,
      unload: unloadImage,
    },
  ];

  return (
    <section className="hero status-rail" aria-label="Server status">
      <div className="status-row status-row-models" role="group" aria-label="Loaded models">
        {modelStatuses.map(({ kind, model, engine: status, owned, busy, unload }) => {
          const unloading = busy === "__unload__";
          const label = model ? model.name || model.id : `No ${kind} Model`;
          return (
            <article className="status-card status-card-current" key={kind} aria-label={`${kind} inference`}>
              <div className="status-copy">
                <strong className="status-value" title={model?.id || label}>
                  <StatusDot tone={status.tone} pulse={status.pulse} />
                  <span style={{ color: model ? "var(--accent)" : "var(--muted-foreground)" }}>
                    {label}
                  </span>
                </strong>
              </div>
              <button
                className="status-action"
                type="button"
                disabled={!owned || Boolean(busy)}
                aria-label={`${unloading ? "Unloading" : "Unload"} active ${kind.toLowerCase()} model`}
                title={owned ? `Unload active ${kind.toLowerCase()} model` : `No ENGRAI-owned ${kind.toLowerCase()} model is loaded`}
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
          );
        })}
      </div>

      <div className="status-row" role="group" aria-label="Server telemetry">
        <article className="status-card">
          <TelemetryBadge kind="gateway" tone={gatewayTone} active={owned} />
          <div className="status-copy">
            <strong className="status-value" style={{ color: active ? "var(--accent)" : "var(--muted-foreground)" }}>{gateway}</strong>
          </div>
        </article>

        <article className="status-card">
          <TelemetryBadge kind="engine" tone={engine.tone} active={engine.pulse} />
          <div className="status-copy">
            <strong className="status-value" style={{ color: engineLoading ? "var(--warn)" : active ? "var(--accent)" : "var(--muted-foreground)" }}>
              {engineLabel}
            </strong>
          </div>
        </article>

        <article className="status-card">
          <TelemetryBadge
            kind="lane"
            tone={laneTone}
            active={laneBusy}
          />
          <div className="status-copy">
            <strong className="status-value" style={{ color: laneColor }}>
              {laneLabel}
            </strong>
          </div>
        </article>
      </div>
    </section>
  );
}
