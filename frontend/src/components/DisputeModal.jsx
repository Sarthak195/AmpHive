/**
 * DisputeModal — let a driver report an issue with a finished charging
 * session. Shared prop contract (redesign-v3):
 *   <DisputeModal open onClose sessionId onSubmitted />
 *
 * Adds a category select (Billing / Charger problem / Access / Other) —
 * the backend only takes a free-text `reason`, so the category is prepended
 * as "[Billing] ..." before it's sent. Submit stays disabled until the
 * driver's own text (excluding the prefix) clears the backend's 10-character
 * minimum. A server rejection — most commonly a 409 when an open dispute
 * already exists for the session — surfaces inline via apiErrorCopy and the
 * modal stays open for another attempt.
 */
import { useEffect, useState } from 'react';
import Modal from './ui/Modal';
import { useToast } from './ui';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';

const MIN_REASON = 10;
const MAX_REASON = 1000;
const CATEGORIES = ['Billing', 'Charger problem', 'Access', 'Other'];

export default function DisputeModal({ open, onClose, sessionId, onSubmitted }) {
  const toast = useToast();
  const [category, setCategory] = useState(CATEGORIES[0]);
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // Fresh form every time the modal opens (or for a different session).
  useEffect(() => {
    if (!open) return;
    setCategory(CATEGORIES[0]);
    setReason('');
    setError('');
    setSubmitting(false);
  }, [open, sessionId]);

  if (!open) return null;

  const trimmedLen = reason.trim().length;
  const tooShort = trimmedLen < MIN_REASON;
  const maxTyped = MAX_REASON - `[${category}] `.length;

  const handleClose = () => {
    if (!submitting) onClose?.();
  };

  const handleSubmit = async () => {
    if (tooShort || submitting) return;
    setError('');
    setSubmitting(true);
    try {
      const created = await api.post(`/api/sessions/${sessionId}/dispute`, {
        reason: `[${category}] ${reason.trim()}`,
      });
      toast.ok("We'll notify you when the operator responds.");
      onSubmitted?.(created);
      onClose?.();
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={handleClose}
      title="Report an issue"
      footer={
        <>
          <button type="button" className="btn btn-quiet" onClick={handleClose} disabled={submitting}>
            Cancel
          </button>
          <button
            type="button"
            className="btn btn-primary"
            onClick={handleSubmit}
            disabled={tooShort || submitting}
          >
            {submitting ? 'Submitting…' : 'Submit'}
          </button>
        </>
      }
    >
      <div className="stack">
        <p className="text-2 text-sm">
          Tell us what went wrong with this session. A CPO will review it.
        </p>

        <div className="field">
          <label className="field-label" htmlFor="dispute-category">
            Category
          </label>
          <select
            id="dispute-category"
            className="select"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label className="field-label" htmlFor="dispute-reason">
            What went wrong?
          </label>
          <textarea
            id="dispute-reason"
            className="textarea"
            rows={4}
            maxLength={maxTyped}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="e.g. The charger stopped after a few minutes but I was billed for the full session."
            aria-invalid={tooShort && reason.length > 0 ? 'true' : undefined}
          />
          <p className="field-help">
            {tooShort
              ? `At least ${MIN_REASON} characters — ${Math.max(0, MIN_REASON - trimmedLen)} to go.`
              : `${trimmedLen}/${maxTyped} characters.`}
          </p>
        </div>

        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </Modal>
  );
}
