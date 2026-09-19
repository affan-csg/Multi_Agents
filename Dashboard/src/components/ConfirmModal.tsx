interface Props {
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  danger?: boolean;
  confirmLabel?: string;
}

export function ConfirmModal({ title, message, onConfirm, onCancel, danger, confirmLabel }: Props) {
  return (
    <div className="drawer-overlay" onClick={onCancel}>
      <div className="confirm-modal" onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        <p>{message}</p>
        <div className="action-row">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className={danger ? "danger" : "primary"} onClick={onConfirm}>
            {confirmLabel || "Continue"}
          </button>
        </div>
      </div>
    </div>
  );
}
