import { useEffect, useState, type FormEvent } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useDisplayFeed } from "../lib/useDisplayFeed";
import { useVersionCheck } from "../lib/useVersionCheck";
import { summarise } from "../lib/engine";
import { gb, tokens } from "../lib/format";
import { Meters } from "../components/Meters";
import { Mark } from "../components/Mark";
import { Button, ErrorNote, Eyebrow, Field, StatusDot } from "../components/primitives";

/* The wall view. Read-only by construction — it renders no control that can
   change the machine, so a monitor in a room full of people cannot be used to
   unload a model by anyone who walks past it. Its credential can only read
   this one endpoint, so the screen cannot be turned into a control surface
   even by someone who takes the token off the disk.

   Sized for a glance from across a room rather than for a pointer: type is set
   in vw so one build fills a 24" desk monitor and a 55" panel. */

export function Display() {
  const { status, snapshot, pair } = useDisplayFeed();
  /* A wall display has nobody to press reload, so it takes the new build as
     soon as it sees one. There is no editing state here to interrupt. */
  const version = useVersionCheck({ autoReload: true });

  useEffect(() => {
    // A wall display is the one place where holding the screen awake is the
    // correct behaviour. The lock drops by itself when the tab is hidden.
    let sentinel: WakeLockSentinel | null = null;
    const acquire = async () => {
      try {
        sentinel = (await navigator.wakeLock?.request("screen")) ?? null;
      } catch {
        // Refused on an insecure origin, and simply absent on older Safari.
      }
    };
    void acquire();
    const onVisible = () => { if (!document.hidden) void acquire(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      void sentinel?.release();
    };
  }, []);

  if (status === "unpaired" || status === "rejected") {
    return <Pairing onPair={pair} rejected={status === "rejected"} />;
  }

  const engine = summarise(snapshot?.engine, snapshot?.runtime);
  const active = snapshot?.active ?? null;
  const stale = status === "offline";

  return (
    <div className="display" data-testid="display">
      <header className="display-head">
        <Mark size={40} />
        <span className="display-brand">ENGRAI SERVER</span>
        <span className="display-status">
          <StatusDot tone={stale ? "bad" : engine.tone} pulse={!stale && engine.pulse} />
          {stale ? "Gateway unreachable" : engine.label}
        </span>
      </header>

      <main className="display-main">
        <Eyebrow>Serving</Eyebrow>
        <AnimatePresence mode="wait" initial={false}>
          <motion.h1
            key={active?.id ?? status}
            className="display-name"
            initial={{ opacity: 0, y: 18 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -18 }}
            transition={{ duration: 0.24 }}
          >
            {status === "connecting" && !snapshot
              ? "Connecting"
              : active
                ? active.name || active.id
                : "Idle"}
          </motion.h1>
        </AnimatePresence>
        <p className="display-detail">
          {stale ? "Showing the last reading before the connection dropped." : engine.detail}
        </p>

        {active && (
          <dl className="display-facts">
            <div><dt>Quant</dt><dd>{active.quantization || "—"}</dd></div>
            <div><dt>Weights</dt><dd>{gb(active.size_gb)}</dd></div>
            <div><dt>Context</dt><dd>{tokens(active.context_tokens)}</dd></div>
            <div><dt>Layers</dt><dd>{active.gpu_layers || "—"}</dd></div>
          </dl>
        )}
      </main>

      <Meters large metrics={snapshot?.metrics ?? null} />

      <footer className="display-foot">
        <span>
          {version.stuck
            ? "Update pinned by a cached copy — clear this display's cache"
            : `${snapshot?.route_count ?? 0} routes registered`}
        </span>
        <span className="display-clock">
          {snapshot?.engine.version ? `Engine ${snapshot.engine.version}` : ""}
        </span>
      </footer>
    </div>
  );
}

function Pairing({
  onPair, rejected,
}: { onPair: (token: string) => Promise<void>; rejected: boolean }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await onPair(value);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not pair this display");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="gate">
      <motion.form
        className="gate-card gate-wide"
        data-testid="pair-form"
        onSubmit={submit}
        initial={{ opacity: 0, y: 12, scale: 0.99 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ type: "spring", stiffness: 380, damping: 32 }}
      >
        <Mark size={52} />
        <Eyebrow>Wall display</Eyebrow>
        <h1>Pair this screen</h1>
        <p className="gate-copy">
          {rejected
            ? "This display's token was revoked. Paste a new one to bring it back."
            : "Paste a display token from Profile & access on the control view. It is stored on this machine and survives a reboot, so this is asked once."}
        </p>
        <Field label="Display token">
          <input
            type="password"
            data-testid="pair-token"
            placeholder="engd-…"
            autoComplete="off"
            value={value}
            required
            data-autofocus
            onChange={(event) => setValue(event.target.value)}
          />
        </Field>
        <ErrorNote>{error}</ErrorNote>
        <Button variant="primary" type="submit" busy={busy}>Pair display</Button>
      </motion.form>
    </div>
  );
}
