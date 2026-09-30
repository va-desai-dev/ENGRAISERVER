import { useEffect } from "react";
import { useStore } from "../store/useStore";

/* A backgrounded phone tab that keeps polling is the reason a web app drains a
   battery and then floods the gateway on resume. This hook polls only while
   the document is visible, and tightens the cadence only while a load or
   unload is actually in flight. */
const IDLE_MS = 5000;
const TRANSITION_MS = 1200;

export function useLiveState() {
  const token = useStore((s) => s.token);
  const phase = useStore((s) => s.snapshot?.runtime.phase);
  const busy = useStore((s) => s.busyModel !== null);

  const transitioning = busy || phase === "starting" || phase === "stopping";

  useEffect(() => {
    if (!token) return;
    const refresh = useStore.getState().refresh;
    let timer: number | undefined;

    const tick = () => {
      if (document.hidden) return;
      void refresh();
    };

    const start = () => {
      window.clearInterval(timer);
      timer = window.setInterval(tick, transitioning ? TRANSITION_MS : IDLE_MS);
    };

    const onVisibility = () => {
      if (document.hidden) {
        window.clearInterval(timer);
      } else {
        // Whatever happened while the phone was in a pocket, show it now
        // rather than after a full interval of stale numbers.
        void refresh();
        start();
      }
    };

    void refresh();
    start();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("online", onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("online", onVisibility);
    };
  }, [token, transitioning]);
}
