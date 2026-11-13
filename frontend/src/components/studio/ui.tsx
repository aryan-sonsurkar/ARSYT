"use client";

import { ReactNode } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, Check, LoaderCircle, X } from "lucide-react";

export function StatusBadge({ state, label }: { state: string; label?: string }) {
  return (
    <span className={`status-badge status-${state}`}>
      <span />
      {label ?? state.replaceAll("_", " ")}
    </span>
  );
}

export function ProgressBar({ value, state }: { value: number; state?: string }) {
  return (
    <div className={`st-progress ${state ?? ""}`} role="progressbar" aria-valuenow={Math.round(value)} aria-valuemin={0} aria-valuemax={100}>
      <span style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
    </div>
  );
}

export function Panel({
  title, subtitle, actions, children, className = "", id,
}: {
  title?: ReactNode; subtitle?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string; id?: string;
}) {
  return (
    <section className={`st-panel ${className}`} id={id}>
      {(title || actions) && (
        <div className="st-panel-head">
          <div>
            {title && <h2>{title}</h2>}
            {subtitle && <p>{subtitle}</p>}
          </div>
          {actions && <div className="st-panel-actions">{actions}</div>}
        </div>
      )}
      {children}
    </section>
  );
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="st-empty">
      <span className="st-empty-icon">{icon}</span>
      <strong>{title}</strong>
      {children && <p>{children}</p>}
    </div>
  );
}

export function Modal({
  open, onClose, title, subtitle, children, width = 560,
}: {
  open: boolean; onClose: () => void; title: ReactNode; subtitle?: ReactNode; children: ReactNode; width?: number;
}) {
  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="st-modal-backdrop"
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
          onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}
        >
          <motion.div
            className="st-modal-card"
            style={{ maxWidth: width }}
            initial={{ opacity: 0, scale: 0.97, y: 12 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98, y: 8 }}
          >
            <div className="st-modal-head">
              <div>
                <h2>{title}</h2>
                {subtitle && <p>{subtitle}</p>}
              </div>
              <button className="icon-button" onClick={onClose} aria-label="Close dialog"><X size={18} /></button>
            </div>
            {children}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="st-field">
      <span>{label}</span>
      {children}
      {hint && <small>{hint}</small>}
    </label>
  );
}

export function Toggle({ checked, onChange, label, hint }: { checked: boolean; onChange: (value: boolean) => void; label: string; hint?: string }) {
  return (
    <label className="st-toggle">
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      <span className="st-toggle-track"><span /></span>
      <span className="st-toggle-copy"><strong>{label}</strong>{hint && <small>{hint}</small>}</span>
    </label>
  );
}

export function Busy({ label = "Working…" }: { label?: string }) {
  return <span className="st-busy"><LoaderCircle size={15} className="spin" />{label}</span>;
}

export function InlineNote({ tone = "info", children }: { tone?: "info" | "warn" | "error" | "ok"; children: ReactNode }) {
  return (
    <div className={`st-note st-note-${tone}`}>
      {tone === "ok" ? <Check size={14} /> : tone === "info" ? null : <AlertTriangle size={14} />}
      <span>{children}</span>
    </div>
  );
}
