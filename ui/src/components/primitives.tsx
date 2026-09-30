import { useState, type ButtonHTMLAttributes, type ReactNode } from "react";
import { motion } from "motion/react";

type Variant = "primary" | "secondary" | "ghost" | "danger";

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  busy?: boolean;
  icon?: ReactNode;
}

export function Button({
  variant = "secondary", busy, icon, children, className = "", disabled, ...rest
}: ButtonProps) {
  return (
    <button
      className={`btn btn-${variant} ${busy ? "is-busy" : ""} ${className}`}
      disabled={disabled || busy}
      {...rest}
    >
      {busy ? <Spinner /> : icon}
      <span>{children}</span>
    </button>
  );
}

export function Spinner() {
  return <span className="spinner" aria-hidden="true" />;
}

/** The one place a state colour is chosen, so nothing drifts. */
export type Tone = "live" | "warm" | "cold" | "bad";

export function StatusDot({ tone, pulse }: { tone: Tone; pulse?: boolean }) {
  return (
    <span className={`dot dot-${tone} ${pulse ? "is-pulsing" : ""}`} aria-hidden="true" />
  );
}

export function Eyebrow({ children }: { children: ReactNode }) {
  return <span className="eyebrow">{children}</span>;
}

/** Copy-to-clipboard that confirms in place rather than through a toast. */
export function CopyField({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="copy-field">
      <code>{value}</code>
      <button
        type="button"
        aria-label={copied ? `${label} copied` : `Copy ${label}`}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(value);
          } catch {
            // Safari refuses the clipboard outside a secure context, which is
            // exactly the plain-http LAN case. Select it so ⌘C still works.
            const range = document.createRange();
            const node = document.activeElement?.previousElementSibling;
            if (node) {
              range.selectNodeContents(node);
              getSelection()?.removeAllRanges();
              getSelection()?.addRange(range);
            }
            return;
          }
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1400);
        }}
      >
        {copied ? (
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6 9 17l-5-5" /></svg>
        ) : (
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <rect x="9" y="9" width="11" height="11" rx="2" />
            <path d="M15 9V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h3" />
          </svg>
        )}
      </button>
    </div>
  );
}

interface FieldProps {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  wide?: boolean;
}

export function Field({ label, hint, children, wide }: FieldProps) {
  return (
    <label className={`field ${wide ? "field-wide" : ""}`}>
      <span className="field-label">{label}</span>
      {children}
      {hint && <small className="field-hint">{hint}</small>}
    </label>
  );
}

export function ErrorNote({ children }: { children?: ReactNode }) {
  if (!children) return null;
  return (
    <motion.p
      className="error-note"
      role="alert"
      initial={{ opacity: 0, y: -4 }}
      animate={{ opacity: 1, y: 0 }}
    >
      {children}
    </motion.p>
  );
}

export function Empty({ title, action }: { title: string; action?: ReactNode }) {
  return (
    <div className="empty-state">
      <p>{title}</p>
      {action}
    </div>
  );
}
