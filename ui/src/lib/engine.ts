import type { Tone } from "../components/primitives";

/* Structural rather than nominal: the admin snapshot and the reduced display
   snapshot both satisfy this, and neither has to know about the other. */
export interface EngineLike {
  reachable: boolean;
  owned: boolean;
  version?: string | null;
  pid?: number | null;
}
export interface RuntimeLike {
  phase?: string;
  target_model?: string | null;
  error?: string | null;
}

export interface EngineSummary {
  tone: Tone;
  label: string;
  detail: string;
  pulse: boolean;
}

/* One place decides what the engine's state is called, so the topbar, the
   sheet and the wall display can never disagree about it. */
export function summarise(
  engine: EngineLike | undefined,
  runtime: RuntimeLike | undefined,
): EngineSummary {
  if (!engine) {
    return { tone: "cold", label: "Unknown", detail: "No status yet.", pulse: false };
  }
  if (runtime?.phase === "starting") {
    return {
      tone: "warm",
      label: "Starting",
      detail: `Bringing up ${runtime.target_model ?? "the runtime"}.`,
      pulse: true,
    };
  }
  if (runtime?.phase === "stopping") {
    return { tone: "warm", label: "Stopping", detail: "Freeing up memory.", pulse: true };
  }
  if (engine.reachable && engine.owned) {
    return {
      tone: "live",
      label: "Live",
      detail: [
        engine.version ? `Engine ${engine.version}` : "Serving",
        engine.pid ? `on pid ${engine.pid}` : "",
      ]
        .filter(Boolean)
        .join(" ") + ".",
      pulse: true,
    };
  }
  if (engine.reachable && !engine.owned) {
    return {
      tone: "warm",
      label: "Foreign",
      // Worth naming plainly: unloading will not touch a process ENGRAI did
      // not start, per the invariant in docs/architecture.md.
      detail: "A runtime ENGRAI did not start is answering on the engine port.",
      pulse: false,
    };
  }
  if (runtime?.error) {
    return { tone: "bad", label: "Failed", detail: runtime.error, pulse: false };
  }
  return {
    tone: "cold",
    label: "Idle",
    detail: "No runtime loaded. The GPU is free.",
    pulse: false,
  };
}
