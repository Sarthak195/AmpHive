/**
 * ErrorState — REQUIRED wherever data is fetched: "Couldn't load" + the
 * friendly detail from apiErrorCopy + a Retry button. Never conflate with
 * EmptyState (which is for true zero-data).
 */

import { AlertTriangle } from 'lucide-react';
import { apiErrorCopy } from '../../utils/statusCopy';

export default function ErrorState({ error, onRetry, title = "Couldn't load this" }) {
  const detail = error ? apiErrorCopy(error) : null;
  return (
    <div className="state-block" role="alert">
      <AlertTriangle className="state-icon" aria-hidden="true" />
      <h3>{title}</h3>
      {detail && <p>{detail}</p>}
      {onRetry && (
        <button type="button" className="btn btn-quiet" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
