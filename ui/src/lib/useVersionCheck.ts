import { useCallback, useEffect, useState } from "react";

/* Detects that the gateway is serving a different build than this page is
   running, and — where it is safe — reloads onto it.

   Hashed assets are served `immutable` for a year. That is what makes the
   interface fast over a tunnel, and it also means a client holding a stale
   index.html keeps fetching its old bundle successfully, from a CDN edge, even
   after the origin has deleted the file. The result is a working but
   permanently out-of-date app showing no symptom at all. A wall display never
   gets a manual reload, so without this it runs its first build forever. */

// This is a single-owner editing surface, so fast save-to-refresh feedback is
// more valuable than minimizing a handful of tiny version requests.
const POLL_MS = 2_000;
// Survives the reload it triggers, which is exactly what makes it a loop
// breaker rather than a counter that resets to zero every time.
const ATTEMPT_KEY = "engrai.reloadAttemptedFor";

/** What this page is actually running, read off the DOM rather than baked in. */
function runningAssets(): string[] {
  const urls = [
    ...document.querySelectorAll<HTMLScriptElement>('script[type="module"][src]'),
  ]
    .map((node) => node.src)
    .concat(
      [
        ...document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"][href]'),
      ].map((node) => node.href),
    );
  return urls
    .map((url) => url.split("/assets/")[1])
    .filter((name): name is string => Boolean(name))
    .sort();
}

function read(key: string): string | null {
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function write(key: string, value: string): void {
  try {
    sessionStorage.setItem(key, value);
  } catch {
    // Private mode. The worst case is a second reload attempt, not a loop:
    // the poll interval is a minute.
  }
}

export interface VersionState {
  /** The gateway is serving a build this page is not running. */
  stale: boolean;
  /** Reloading was already tried for this build and did not take. */
  stuck: boolean;
  reload(): void;
}

export function useVersionCheck({ autoReload }: { autoReload: boolean }): VersionState {
  const [stale, setStale] = useState(false);
  const [stuck, setStuck] = useState(false);

  const reload = useCallback(() => {
    // Give the HTML document a distinct URL so a stale browser or Cloudflare
    // copy cannot pin the previous asset hashes. The gateway ignores the
    // query string, so this works for both / and /display.
    const url = new URL(window.location.href);
    url.searchParams.set("_engrai_ui", Date.now().toString(36));
    window.location.replace(url);
  }, []);

  useEffect(() => {
    const running = runningAssets();
    // The dev server serves /src/main.tsx, so there is no hashed bundle to
    // compare and nothing to police.
    if (running.length === 0) return;

    let cancelled = false;

    const check = async () => {
      if (document.hidden) return;
      let served: string[];
      try {
        const response = await fetch("/control/version", {
          cache: "no-store",
          credentials: "omit",
        });
        if (!response.ok) return;
        served = ((await response.json()) as { assets: string[] }).assets ?? [];
      } catch {
        // A dropped tunnel is not a version problem.
        return;
      }
      if (cancelled || served.length === 0) return;

      const signature = served.join(",");
      if (signature === running.join(",")) {
        setStale(false);
        return;
      }
      setStale(true);

      // If a reload was already attempted for this exact build and we are
      // still on the old one, something upstream is pinning it — a CDN edge
      // holding the shell, most likely. Reloading again would spin forever,
      // so stop and let a person decide.
      if (read(ATTEMPT_KEY) === signature) {
        setStuck(true);
        return;
      }
      if (autoReload) {
        write(ATTEMPT_KEY, signature);
        reload();
      }
    };

    void check();
    const timer = window.setInterval(() => { void check(); }, POLL_MS);
    const onVisible = () => { if (!document.hidden) void check(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [autoReload, reload]);

  return { stale, stuck, reload };
}
