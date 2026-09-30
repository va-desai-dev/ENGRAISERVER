interface MeterProps {
  label: string;
  percent: number | null;
  value: string;
  sub?: string;
  /** Display mode uses the same component at twice the scale. */
  large?: boolean;
}

/* Thresholds live here rather than in a colour prop, so "hot" means the same
   thing on every meter in the app. */
function band(percent: number | null): string {
  if (percent == null) return "unknown";
  if (percent >= 90) return "critical";
  if (percent >= 75) return "hot";
  return "normal";
}

export function Meter({ label, percent, value, sub, large }: MeterProps) {
  const state = band(percent);
  return (
    <div className={`meter meter-${state} ${large ? "meter-large" : ""}`}>
      <span className="meter-label" title={label}>{label}</span>
      <strong className="meter-value">{value}</strong>
      <div className="meter-track">
        <span
          className="meter-fill"
          style={{ transform: `scaleX(${Math.min(100, Math.max(0, percent ?? 0)) / 100})` }}
        />
      </div>
      {sub && <span className="meter-sub" title={sub}>{sub}</span>}
    </div>
  );
}
