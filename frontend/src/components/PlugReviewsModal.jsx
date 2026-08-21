/**
 * PlugReviewsModal — the "Photos & reviews" panel opened from a map popup /
 * list row. Mirrors ReportProblemModal's shape (Modal + useToast +
 * apiErrorCopy, form reset on open) but reads/writes plug reviews.
 *
 * Reads are PUBLIC (GET /api/plugs/{id}/reviews works signed-out), so the list
 * shows for everyone. WRITING is verified-charger-only server-side: we show the
 * star+text form to any signed-in user and let the backend's 403 surface inline
 * as "you can only review a charger you've completed a session at" — the client
 * can't know session history, and duplicating that gate here would just drift.
 * Anonymous visitors get a "Sign in to review" CTA routing to /login?next=/map
 * (same handoff as MapPage.handleSelectPlug).
 *
 * `onSaved(agg)` lets the parent patch the map/list aggregate (avg_rating,
 * review_count) optimistically after a write without a full refetch.
 */
import { useEffect, useState, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { ImagePlus } from 'lucide-react';
import Modal from './ui/Modal';
import { StarRating, Skeleton, EmptyState, useToast } from './ui';
import { useAuth } from '../contexts/AuthContext';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';
import './PlugReviewsModal.css';

const MAX_BODY = 1000;

export default function PlugReviewsModal({ open, onClose, plug, onSaved }) {
  const { user } = useAuth();
  const navigate = useNavigate();
  const toast = useToast();
  const plugId = plug?.id;

  const [reviews, setReviews] = useState([]);
  const [photos, setPhotos] = useState([]);
  const [uploading, setUploading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');

  const [rating, setRating] = useState(0);
  const [body, setBody] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const load = useCallback(async () => {
    if (!plugId) return;
    setLoading(true);
    setLoadError('');
    try {
      const [reviewData, photoData] = await Promise.all([
        api.get(`/api/plugs/${plugId}/reviews`),
        api.get(`/api/plugs/${plugId}/photos`),
      ]);
      setReviews(reviewData);
      setPhotos(photoData);
      // Pre-fill the form from the caller's existing review, if any.
      const mine = reviewData.find((r) => r.is_mine);
      setRating(mine ? mine.rating : 0);
      setBody(mine ? mine.body ?? '' : '');
    } catch (err) {
      setLoadError(apiErrorCopy(err));
    } finally {
      setLoading(false);
    }
  }, [plugId]);

  useEffect(() => {
    if (!open) return;
    setError('');
    setSubmitting(false);
    load();
  }, [open, plugId, load]);

  if (!open) return null;

  const mine = reviews.find((r) => r.is_mine);
  const others = reviews.filter((r) => !r.is_mine);
  const count = reviews.length;
  const avg =
    count > 0 ? reviews.reduce((s, r) => s + r.rating, 0) / count : null;

  const handleClose = () => {
    if (!submitting) onClose?.();
  };

  const handleSubmit = async () => {
    if (rating < 1 || submitting) return;
    setError('');
    setSubmitting(true);
    try {
      const saved = await api.post(`/api/plugs/${plugId}/reviews`, {
        rating,
        body: body.trim() || null,
      });
      toast.ok(mine ? 'Review updated.' : 'Thanks for your review!');
      // Recompute the aggregate locally and hand it back to the parent.
      const next = [saved, ...reviews.filter((r) => !r.is_mine)];
      const nextAvg = next.reduce((s, r) => s + r.rating, 0) / next.length;
      onSaved?.({ avg_rating: Math.round(nextAvg * 10) / 10, review_count: next.length });
      setReviews(next);
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setSubmitting(false);
    }
  };

  const handleDelete = async () => {
    if (submitting) return;
    setSubmitting(true);
    try {
      await api.delete(`/api/plugs/${plugId}/reviews/mine`);
      const next = reviews.filter((r) => !r.is_mine);
      const nextAvg =
        next.length > 0 ? next.reduce((s, r) => s + r.rating, 0) / next.length : null;
      onSaved?.({
        avg_rating: nextAvg == null ? null : Math.round(nextAvg * 10) / 10,
        review_count: next.length,
      });
      setReviews(next);
      setRating(0);
      setBody('');
      toast.ok('Your review was removed.');
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setSubmitting(false);
    }
  };

  const handlePhotoPick = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ''; // allow re-picking the same file
    if (!file || uploading) return;
    if (file.size > 8 * 1024 * 1024) {
      setError('That photo is larger than 8 MB — please pick a smaller one.');
      return;
    }
    setError('');
    setUploading(true);
    try {
      const saved = await api.upload(`/api/plugs/${plugId}/photos`, file);
      setPhotos((prev) => [saved, ...prev]);
      toast.ok("Photo submitted — it'll appear once an operator approves it.");
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setUploading(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={handleClose}
      size="md"
      title={plug?.name ? `Reviews · ${plug.name}` : 'Reviews'}
      footer={
        <button type="button" className="btn btn-quiet" onClick={handleClose} disabled={submitting}>
          Close
        </button>
      }
    >
      <div className="stack plug-reviews">
        {/* Aggregate header */}
        <div className="plug-reviews-agg">
          {count > 0 ? (
            <>
              <StarRating value={avg} size={18} />
              <span className="plug-reviews-agg-num">
                {avg.toFixed(1)} · {count} review{count === 1 ? '' : 's'}
              </span>
            </>
          ) : (
            <span className="text-2 text-sm">No reviews yet.</span>
          )}
        </div>

        {/* Photo gallery + upload */}
        {(photos.length > 0 || user) && (
          <div className="plug-reviews-photos">
            {photos.length > 0 && (
              <div className="plug-photo-grid">
                {photos.map((p) => (
                  <a
                    key={p.id}
                    href={p.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className={`plug-photo${p.status === 'pending' ? ' is-pending' : ''}`}
                  >
                    <img src={p.url} alt={plug?.name ?? 'Charger'} loading="lazy" />
                    {p.status === 'pending' && (
                      <span className="plug-photo-badge">Pending review</span>
                    )}
                  </a>
                ))}
              </div>
            )}
            {user && (
              <label className={`btn btn-quiet btn-sm plug-photo-upload${uploading ? ' is-busy' : ''}`}>
                <ImagePlus size={14} aria-hidden="true" />
                {uploading ? 'Uploading…' : 'Add a photo'}
                <input
                  type="file"
                  accept="image/jpeg,image/png,image/webp"
                  onChange={handlePhotoPick}
                  disabled={uploading}
                  hidden
                />
              </label>
            )}
          </div>
        )}

        {/* Write form (signed-in) or sign-in CTA */}
        {user ? (
          <div className="field plug-reviews-form">
            <label className="field-label">{mine ? 'Your review' : 'Leave a review'}</label>
            <StarRating value={rating} onChange={setRating} size={22} />
            <textarea
              className="textarea"
              rows={3}
              maxLength={MAX_BODY}
              value={body}
              onChange={(e) => setBody(e.target.value)}
              placeholder="How was charging here? (optional)"
            />
            <div className="modal-actions">
              {mine && (
                <button
                  type="button"
                  className="btn btn-ghost btn-sm"
                  onClick={handleDelete}
                  disabled={submitting}
                >
                  Delete
                </button>
              )}
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={handleSubmit}
                disabled={rating < 1 || submitting}
              >
                {submitting ? 'Saving…' : mine ? 'Update' : 'Submit'}
              </button>
            </div>
            {error && (
              <p className="field-error" role="alert">
                {error}
              </p>
            )}
          </div>
        ) : (
          <button
            type="button"
            className="btn btn-primary btn-sm"
            onClick={() => navigate(`/login?next=${encodeURIComponent('/map')}`)}
          >
            Sign in to review
          </button>
        )}

        {/* Review list */}
        <div className="plug-reviews-list">
          {loading ? (
            <Skeleton lines={3} />
          ) : loadError ? (
            <p className="field-error" role="alert">{loadError}</p>
          ) : count === 0 ? (
            <EmptyState title="Be the first to review this charger." />
          ) : (
            [...(mine ? [mine] : []), ...others].map((r) => (
              <div key={r.id} className="plug-review-item">
                <div className="plug-review-head">
                  <StarRating value={r.rating} size={14} />
                  <span className="plug-review-author">
                    {r.is_mine ? 'You' : r.author_display}
                  </span>
                </div>
                {r.body && <p className="plug-review-body">{r.body}</p>}
              </div>
            ))
          )}
        </div>
      </div>
    </Modal>
  );
}
