import { AnimatePresence, motion } from "motion/react";
import { useStore } from "../store/useStore";
import { time } from "../lib/format";

export function Activity() {
  const log = useStore((s) => s.log);

  return (
    <section className="panel activity">
      <div className="panel-head">
        <div>
          <span className="eyebrow">Control log</span>
          <h2>Activity</h2>
        </div>
        <span className="live-chip"><i /> Live</span>
      </div>
      <ol className="activity-list">
        <AnimatePresence initial={false}>
          {log.map((entry) => (
            <motion.li
              key={entry.id}
              className={`tone-${entry.tone}`}
              initial={{ opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.16 }}
            >
              <time>{time(entry.at)}</time>
              <span>{entry.message}</span>
            </motion.li>
          ))}
        </AnimatePresence>
        {log.length === 0 && (
          <li className="is-empty"><span>Nothing yet this session.</span></li>
        )}
      </ol>
    </section>
  );
}
