import { useEffect, useId, useRef, type ReactNode } from "react";
import { AnimatePresence, motion } from "motion/react";

/* Replaces the `popover` attribute, which is inert on iOS 16 and therefore on
   a large share of the iPads that make plausible wall displays. On a phone
   this presents as a bottom sheet; on a monitor as a centred dialog. */

interface SheetProps {
  open: boolean;
  onClose: () => void;
  title: string;
  eyebrow?: string;
  children: ReactNode;
  footer?: ReactNode;
  /** Narrow sheets read better for confirmations and status. */
  size?: "sm" | "md" | "lg";
}

/* iOS ignores `overflow: hidden` on body, so the page is pinned in place and
   its scroll offset restored on close. Without this, dismissing a sheet
   silently returns the user to the top of the page. */
function useScrollLock(active: boolean) {
  useEffect(() => {
    if (!active) return;
    const { scrollY } = window;
    const { body } = document;
    const previous = body.style.cssText;
    body.style.position = "fixed";
    body.style.top = `-${scrollY}px`;
    body.style.left = "0";
    body.style.right = "0";
    body.style.width = "100%";
    return () => {
      body.style.cssText = previous;
      window.scrollTo(0, scrollY);
    };
  }, [active]);
}

export function Sheet({
  open, onClose, title, eyebrow, children, footer, size = "md",
}: SheetProps) {
  const labelId = useId();
  const panel = useRef<HTMLDivElement>(null);
  useScrollLock(open);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      // Keep Tab inside the sheet; a dialog that lets focus wander behind it
      // is the difference between a component and a div.
      const focusable = panel.current.querySelectorAll<HTMLElement>(
        'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])',
      );
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => document.removeEventListener("keydown", onKey, true);
  }, [open, onClose]);

  useEffect(() => {
    if (!open || !panel.current) return;
    const target = panel.current.querySelector<HTMLElement>("[data-autofocus]");
    // Focusing a text field on a phone throws up the keyboard and covers the
    // sheet, so only an explicit opt-in gets focus.
    target?.focus();
  }, [open]);

  return (
    <AnimatePresence>
      {open && (
        <div className="sheet-layer">
          <motion.div
            className="sheet-scrim"
            onClick={onClose}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.16 }}
          />
          <motion.div
            ref={panel}
            className={`sheet sheet-${size}`}
            role="dialog"
            aria-modal="true"
            aria-labelledby={labelId}
            initial={{ opacity: 0, y: 24, scale: 0.985 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: 16, scale: 0.99 }}
            transition={{ type: "spring", stiffness: 420, damping: 34, mass: 0.7 }}
          >
            <header className="sheet-head">
              <div>
                {eyebrow && <span className="eyebrow" title={eyebrow}>{eyebrow}</span>}
                <h2 id={labelId} title={title}>{title}</h2>
              </div>
              <button
                className="icon-button"
                type="button"
                onClick={onClose}
                aria-label={`Close ${title}`}
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M6 6l12 12M18 6L6 18" />
                </svg>
              </button>
            </header>
            <div className="sheet-body">{children}</div>
            {footer && <footer className="sheet-foot">{footer}</footer>}
          </motion.div>
        </div>
      )}
    </AnimatePresence>
  );
}
