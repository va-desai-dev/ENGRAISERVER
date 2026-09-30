import { useCallback, useEffect, useState } from "react";
import { ApiError } from "./api";
import type { DisplaySnapshot } from "./types";

/* The wall display's own data path, deliberately independent of the control
   store.

   A monitor bolted to a wall must survive a power cut without someone finding
   a keyboard, so its credential lives in localStorage rather than in the
   tab-scoped sessionStorage the control view uses. That token is read-only and
   reaches exactly one endpoint, which is what makes leaving it on disk in a
   shared room an acceptable trade rather than a careless one. */

const DISPLAY_TOKEN = "engrai.display";
const SESSION_TOKEN = "engrai.session";
const POLL_MS = 5000;

function readToken(): string | null {
  try {
    // An admin already signed in on this machine can preview the wall view
    // without pairing it first.
    return localStorage.getItem(DISPLAY_TOKEN) ?? sessionStorage.getItem(SESSION_TOKEN);
  } catch {
    // Private mode, or storage disabled for the origin.
    return null;
  }
}

export type FeedStatus = "unpaired" | "connecting" | "live" | "offline" | "rejected";

export interface Feed {
  status: FeedStatus;
  snapshot: DisplaySnapshot | null;
  pair(token: string): Promise<void>;
}

export function useDisplayFeed(): Feed {
  const [token, setToken] = useState<string | null>(readToken);
  const [snapshot, setSnapshot] = useState<DisplaySnapshot | null>(null);
  const [status, setStatus] = useState<FeedStatus>(token ? "connecting" : "unpaired");

  const fetchOnce = useCallback(async (candidate: string): Promise<DisplaySnapshot> => {
    const response = await fetch("/control/display", {
      headers: { Authorization: `Bearer ${candidate}` },
      credentials: "omit",
    });
    if (response.status === 401 || response.status === 403) {
      throw new ApiError(response.status, "This display is not authorised");
    }
    if (!response.ok) throw new ApiError(response.status, "Gateway error");
    return (await response.json()) as DisplaySnapshot;
  }, []);

  const pair = useCallback(
    async (candidate: string) => {
      const trimmed = candidate.trim();
      // Verified before it is stored, so a mistyped token cannot leave the
      // display in a permanently broken state that looks like an outage.
      const first = await fetchOnce(trimmed);
      try {
        localStorage.setItem(DISPLAY_TOKEN, trimmed);
      } catch {
        // Still usable for this session even where storage is unavailable.
      }
      setSnapshot(first);
      setToken(trimmed);
      setStatus("live");
    },
    [fetchOnce],
  );

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    let timer: number | undefined;

    const tick = async () => {
      if (document.hidden) return;
      try {
        const next = await fetchOnce(token);
        if (cancelled) return;
        setSnapshot(next);
        setStatus("live");
      } catch (error) {
        if (cancelled) return;
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          // A revoked token must stop the display rather than let it sit
          // showing figures from before it was turned off.
          try { localStorage.removeItem(DISPLAY_TOKEN); } catch { /* ignore */ }
          setStatus("rejected");
          setToken(null);
          setSnapshot(null);
          return;
        }
        // A tunnel that drops is normal; keep the last good reading on screen
        // and say plainly that it is no longer current.
        setStatus("offline");
      }
    };

    void tick();
    timer = window.setInterval(() => { void tick(); }, POLL_MS);
    const onVisible = () => { if (!document.hidden) void tick(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [token, fetchOnce]);

  return { status, snapshot, pair };
}
