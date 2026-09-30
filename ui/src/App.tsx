import { useEffect } from "react";
import { AnimatePresence, motion } from "motion/react";
import { useStore } from "./store/useStore";
import { Control } from "./modes/Control";
import { Display } from "./modes/Display";
import { Gate } from "./components/Gate";
import { Mark } from "./components/Mark";

/* Two modes, one build. /display is the read-only wall view for the machine
   hooked to a monitor; everything else is the control interface. This is a
   pathname check rather than a router because there are two routes and a
   router would be the largest dependency in the bundle. */
function mode(): "display" | "control" {
  return window.location.pathname.replace(/\/+$/, "") === "/display"
    ? "display"
    : "control";
}

export function App() {
  const screen = useStore((s) => s.screen);
  const boot = useStore((s) => s.boot);
  const wall = mode() === "display";

  useEffect(() => {
    // The wall view authenticates with its own display token, so booting an
    // admin session here would put a password prompt on a public screen.
    if (!wall) void boot();
  }, [boot, wall]);

  if (wall) return <Display />;

  return (
    <AnimatePresence mode="wait">
      {screen === "loading" && (
        <motion.div key="loading" className="boot" exit={{ opacity: 0 }}>
          <Mark size={44} />
        </motion.div>
      )}
      {(screen === "locked" || screen === "setup") && (
        <motion.div key="gate" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
          <Gate />
        </motion.div>
      )}
      {screen === "ready" && (
        <motion.div key="control" initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
          <Control />
        </motion.div>
      )}
    </AnimatePresence>
  );
}
