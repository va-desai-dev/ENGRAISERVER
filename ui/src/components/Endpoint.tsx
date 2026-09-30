import { useStore } from "../store/useStore";
import { CopyField } from "./primitives";

/* What a client needs in order to talk to this gateway, taken from the page's
   own origin so it is correct behind a tunnel as well as on the LAN. */
export function Endpoint() {
  const snapshot = useStore((s) => s.snapshot);
  const active = snapshot?.models.find((model) => model.active);
  const sample = active?.id ?? snapshot?.models[0]?.id ?? "your-route-id";
  const base = `${window.location.origin}/v1`;

  return (
    <section className="panel endpoint">
      <div className="panel-head">
        <div>
          <span className="eyebrow">OpenAI endpoint</span>
          <h2>Connection</h2>
        </div>
        <span className="auth-chip">Bearer</span>
      </div>

      <span className="field-label">Base URL</span>
      <CopyField value={base} label="base URL" />

      <span className="field-label">Request</span>
      <pre className="code-block">
{`{
  "model": "${sample}",
  "messages": [ … ],
  "stream": true
}`}
      </pre>

      <div className="route-path" aria-hidden="true">
        <span>Client</span><i /><span>ENGRAI SERVER</span><i /><span>Engine</span>
      </div>
    </section>
  );
}
