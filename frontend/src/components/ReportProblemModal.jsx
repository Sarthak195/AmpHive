/**
 * ReportProblemModal — "Report a problem with this charger" (near-copy of
 * DisputeModal.jsx, minus the session/refund framing). Shared prop contract:
 *   <ReportProblemModal open onClose plugId plugName onSubmitted />
 *
 * Unlike DisputeModal (which prepends a category prefix to a single free-text
 * `reason` because the backend only accepts one field), the backend here
 * validates `category` as its own enum-like field
 * (backend/schemas.py PLUG_REPORT_CATEGORIES) — so this posts
 * `{ category, description }` directly, no prefix hack. Submit stays disabled
 * until `description` clears the backend's 10-character minimum. A server
 * rejection surfaces inline via apiErrorCopy and the modal stays open for
 * another attempt.
 */
import { useEffect, useState } from 'react';
import Modal from './ui/Modal';
import { useToast } from './ui';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';

const MIN_DESCRIPTION = 10;
const MAX_DESCRIPTION = 1000;

const CATEGORIES = [
  { value: 'damaged', label: 'Physically damaged' },
  { value: 'wrong_info', label: 'Wrong info (name, price, location)' },
  { value: 'unsafe', label: 'Unsafe' },
  { value: 'other', label: 'Other' },
];

export default function ReportProblemModal({ open, onClose, plugId, plugName, onSubmitted }) {
  const toast = useToast();
  const [category, setCategory] = useState(CATEGORIES[0].value);
  const [description, setDescription] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // Fresh form every time the modal opens (or for a different plug).
  useEffect(() => {
    if (!open) return;
    setCategory(CATEGORIES[0].value);
    setDescription('');
    setError('');
    setSubmitting(false);
  }, [open, plugId]);

  if (!open) return null;

  const trimmedLen = description.trim().length;
  const tooShort = trimmedLen < MIN_DESCRIPTION;

  const handleClose = () => {
    if (!submitting) onClose?.();
  };

  const handleSubmit = async () => {
    if (tooShort || submitting) return;
    setError('');
    setSubmitting(true);
    try {
      const created = await api.post(`/api/plugs/${plugId}/report`, {
        category,
        description: description.trim(),
      });
      toast.ok("Thanks — we've flagged this for the operator.");
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
      title="Report a problem"
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
          {plugName ? `What's wrong with ${plugName}?` : "What's wrong with this charger?"} The
          operator will be notified — no need to have charged here first.
        </p>

        <div className="field">
          <label className="field-label" htmlFor="report-category">
            Category
          </label>
          <select
            id="report-category"
            className="select"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          >
            {CATEGORIES.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label className="field-label" htmlFor="report-description">
            Details
          </label>
          <textarea
            id="report-description"
            className="textarea"
            rows={4}
            maxLength={MAX_DESCRIPTION}
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="e.g. The connector is cracked and sparks when I plug in."
            aria-invalid={tooShort && description.length > 0 ? 'true' : undefined}
          />
          <p className="field-help">
            {tooShort
              ? `At least ${MIN_DESCRIPTION} characters — ${Math.max(0, MIN_DESCRIPTION - trimmedLen)} to go.`
              : `${trimmedLen}/${MAX_DESCRIPTION} characters.`}
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
