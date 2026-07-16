/**
 * DisputeModal — let a driver report an issue with a finished charging session.
 *
 * Files a dispute against a session (POST /api/sessions/{id}/dispute with a
 * free-text {reason}) for a CPO to review. The backend requires the reason to
 * be 10–1000 characters, so Submit stays disabled until the trimmed reason is
 * long enough. Server rejections — most commonly a 409 when an open dispute
 * already exists for the session — surface inline via the api client's error
 * message and the modal stays open for another attempt.
 */
import { useState } from 'react';
import api from '../api/client';

const MIN_REASON = 10;
const MAX_REASON = 1000;

export default function DisputeModal({ sessionId, onClose, onSubmitted }) {
  const [reason, setReason] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const trimmedLen = reason.trim().length;
  const tooShort = trimmedLen < MIN_REASON;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    if (tooShort) return;

    setSubmitting(true);
    try {
      const created = await api.post(`/api/sessions/${sessionId}/dispute`, {
        reason: reason.trim(),
      });
      onSubmitted?.(created);
      onClose();
    } catch (err) {
      // 409 "an open dispute already exists" (and other policy rejections)
      // land here with the backend's specific message.
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal-content" onClick={(e) => e.stopPropagation()}>
        <div className="modal-header">
          <h2>Report an issue</h2>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>

        <p style={{ fontSize: '0.85rem', color: 'var(--color-text-secondary)', marginBottom: '1rem' }}>
          Tell us what went wrong with this session. A CPO will review it.
        </p>

        <form onSubmit={handleSubmit}>
          <label style={{ display: 'block', fontSize: '0.85rem', marginBottom: '0.75rem' }}>
            What went wrong?
            <textarea
              className="input"
              rows={4}
              maxLength={MAX_REASON}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. The charger stopped after a few minutes but I was billed for the full session."
              style={{ width: '100%', marginTop: '0.25rem', resize: 'vertical' }}
            />
          </label>

          <div
            style={{ fontSize: '0.8rem', color: 'var(--color-text-muted)', marginBottom: '0.75rem' }}
          >
            {tooShort
              ? `At least ${MIN_REASON} characters — ${Math.max(0, MIN_REASON - trimmedLen)} to go.`
              : `${trimmedLen}/${MAX_REASON} characters.`}
          </div>

          {error && <div className="error-text mt-2" style={{ marginBottom: '0.75rem' }}>{error}</div>}

          <div className="modal-actions">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-accent" disabled={tooShort || submitting}>
              {submitting ? '...' : 'Submit'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
