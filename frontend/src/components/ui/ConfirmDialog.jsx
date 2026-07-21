/**
 * ConfirmDialog — replaces every window.confirm in the app. Built on Modal;
 * states the concrete consequence in `body`, confirms with a solid danger or
 * primary button, and shows a working label (disabling both buttons) while
 * `busy`.
 */

import Modal from './Modal';

export default function ConfirmDialog({
  open,
  onClose,
  onConfirm,
  title,
  body,
  confirmLabel = 'Confirm',
  tone = 'danger',
  busy = false,
  busyLabel = 'Working…',
}) {
  const handleClose = () => {
    if (!busy) onClose?.();
  };

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title={title}
      size="sm"
      footer={
        <>
          <button type="button" className="btn btn-quiet" onClick={handleClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className={tone === 'primary' ? 'btn btn-primary' : 'btn btn-danger-solid'}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </>
      }
    >
      {typeof body === 'string' ? <p className="text-2">{body}</p> : body}
    </Modal>
  );
}
