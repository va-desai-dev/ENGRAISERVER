import { motion } from "motion/react";
import type { ImageModelProfile } from "../lib/types";
import { gb } from "../lib/format";
import { Button } from "./primitives";

interface Props {
  model: ImageModelProfile;
  busy: boolean;
  disabled: boolean;
  onLoad: () => void;
  onEdit: () => void;
}

export function ImageModelCard({ model, busy, disabled, onLoad, onEdit }: Props) {
  const broken = model.missing_paths.length > 0;
  return (
    <motion.article
      className={`card ${model.active ? "is-active" : ""} ${broken ? "is-broken" : ""}`}
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 380, damping: 34 }}
    >
      <header className="card-head">
        <div className="card-title">
          <h3>{model.name || model.id}</h3>
          <code>{model.id}</code>
        </div>
        {model.active && <span className="card-badge">Live</span>}
      </header>

      <dl className="card-facts">
        <div><dt>Quant</dt><dd>{model.quantization}</dd></div>
        <div><dt>Size</dt><dd>{gb(model.size_gb)}</dd></div>
        <div><dt>GPU</dt><dd>{model.gpu}</dd></div>
        <div><dt>VRAM cap</dt><dd>{model.vram_limit_mib} MiB</dd></div>
      </dl>

      {broken && <p className="card-warning">{model.missing_paths.length} required file(s) missing.</p>}
      {model.notes && <p className="card-notes">{model.notes}</p>}

      <footer className="card-foot">
        <Button variant="ghost" onClick={onEdit}>Edit</Button>
        <Button
          variant={model.active ? "secondary" : "primary"}
          busy={busy}
          disabled={disabled || broken || model.active}
          onClick={onLoad}
        >
          {model.active ? "Loaded" : "Load"}
        </Button>
      </footer>
    </motion.article>
  );
}
