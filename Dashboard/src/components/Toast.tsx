import { useEffect } from "react";
import { Icon, type IconName } from "./ui";

export interface ToastData {
  id: string;
  tone: "success" | "error";
  title: string;
  message?: string;
}

interface ToastProps {
  toast: ToastData;
  onDismiss: (id: string) => void;
  durationMs?: number;
}

const TONE_ICON: Record<ToastData["tone"], IconName> = {
  success: "check",
  error: "close",
};

function Toast({ toast, onDismiss, durationMs = 7000 }: ToastProps) {
  useEffect(() => {
    const t = setTimeout(() => onDismiss(toast.id), durationMs);
    return () => clearTimeout(t);
  }, [toast.id, durationMs, onDismiss]);

  return (
    <div className={`toast toast--${toast.tone}`} role="status">
      <span className="toast-icon">
        <Icon name={TONE_ICON[toast.tone]} size={15} />
      </span>
      <div className="toast-body">
        <div className="toast-title">{toast.title}</div>
        {toast.message && <p className="toast-message">{toast.message}</p>}
      </div>
      <button type="button" className="toast-close" aria-label="Dismiss" onClick={() => onDismiss(toast.id)}>
        <Icon name="close" size={13} />
      </button>
    </div>
  );
}

export function ToastStack({ toasts, onDismiss }: { toasts: ToastData[]; onDismiss: (id: string) => void }) {
  if (toasts.length === 0) return null;
  return (
    <div className="toast-stack">
      {toasts.map((t) => (
        <Toast key={t.id} toast={t} onDismiss={onDismiss} />
      ))}
    </div>
  );
}
