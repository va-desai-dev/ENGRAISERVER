import { useStore } from "../store/useStore";
import type { Metrics } from "../lib/types";
import { gib } from "../lib/format";
import { Meter } from "./Meter";

/* Host resources, one tile per real thing rather than one per number: a GPU is
   a single tile whose headline is VRAM, because VRAM is what decides whether
   the next model fits. Core load and temperature ride along underneath.

   The control view spreads fixed-height instruments across wrapping rows;
   the wall display keeps the larger presentation. */
export function Meters({
  large,
  metrics: supplied,
}: {
  large?: boolean;
  /** The wall view passes its own; the control view reads the store. */
  metrics?: Metrics | null;
}) {
  const fromStore = useStore((s) => s.metrics);
  const metrics = supplied ?? fromStore;
  if (!metrics) return null;

  const { cpu, memory, gpus } = metrics;

  return (
    <div className="meters" aria-label="Host resources">
      {gpus.map((gpu) => (
        <Meter
          key={gpu.index}
          large={large}
          label={gpus.length > 1 ? `GPU ${gpu.index} · VRAM` : "VRAM"}
          percent={gpu.percent}
          value={`${Math.round(gpu.percent)}%`}
          sub={`${gib(gpu.used_mib)} / ${gib(gpu.total_mib)} · ${gpu.utilization}% · ${gpu.temperature}°`}
        />
      ))}
      <Meter
        large={large}
        label="CPU"
        percent={cpu.percent}
        value={cpu.percent == null ? "—" : `${Math.round(cpu.percent)}%`}
      />
      {memory && (
        <Meter
          large={large}
          label="RAM"
          percent={memory.percent}
          value={`${Math.round(memory.percent)}%`}
          sub={`${gib(memory.used_mib)} / ${gib(memory.total_mib)}`}
        />
      )}
    </div>
  );
}
