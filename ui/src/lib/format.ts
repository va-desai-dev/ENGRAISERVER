/** Bytes-ish helpers and clock formatting, kept out of components. */

export function gib(mib: number): string {
  return `${(mib / 1024).toFixed(mib < 10240 ? 1 : 0)} GB`;
}

export function gb(value: number | null): string {
  return value == null ? "—" : `${value.toFixed(1)} GB`;
}

export function tokens(count: number): string {
  if (!count) return "—";
  if (count >= 1000) {
    const k = count / 1000;
    return `${Number.isInteger(k) ? k : k.toFixed(1)}K`;
  }
  return String(count);
}

const clock = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

export function time(ms: number): string {
  return clock.format(ms);
}

/** "3m ago" — coarse on purpose; this is a glanceable field, not an audit log. */
export function since(seconds: number): string {
  if (!seconds) return "never";
  const delta = Date.now() / 1000 - seconds;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

/** Collapse a long absolute path to something that fits one line. */
export function shortPath(path: string, keep = 2): string {
  const parts = path.split("/").filter(Boolean);
  if (parts.length <= keep) return path;
  return `…/${parts.slice(-keep).join("/")}`;
}
