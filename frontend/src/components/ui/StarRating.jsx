/**
 * StarRating — two modes on one component, following the app's lucide `Star`
 * fill idiom (fill="currentColor" vs "none", same as the favorite star in
 * MapComponent.jsx).
 *
 *  - Display (default, `value` set, no `onChange`): read-only stars for an
 *    aggregate/average. Supports fractional averages via a clipped overlay so
 *    "4.5" shows a half-filled fourth star. `aria-label` announces the value.
 *  - Input (`onChange` provided): interactive 1..5 whole-star picker with
 *    hover preview and keyboard (arrow keys / 1-5) support, rendered as a
 *    radiogroup for a11y.
 */
import { useState } from 'react';
import { Star } from 'lucide-react';
import './StarRating.css';

export default function StarRating({
  value = 0,
  onChange,
  size = 16,
  max = 5,
  label,
  className = '',
}) {
  const [hover, setHover] = useState(0);
  const interactive = typeof onChange === 'function';

  if (!interactive) {
    // Read-only, fractional-aware display.
    const rounded = Math.round((value || 0) * 2) / 2;
    return (
      <span
        className={`star-rating star-rating--display ${className}`}
        role="img"
        aria-label={label ?? `Rated ${value} out of ${max}`}
      >
        {Array.from({ length: max }, (_, i) => {
          const fillFrac = Math.max(0, Math.min(1, rounded - i));
          return (
            <span key={i} className="star-rating-slot" aria-hidden="true">
              <Star size={size} fill="none" />
              {fillFrac > 0 && (
                <span
                  className="star-rating-fill"
                  style={{ width: `${fillFrac * 100}%` }}
                >
                  <Star size={size} fill="currentColor" />
                </span>
              )}
            </span>
          );
        })}
      </span>
    );
  }

  const shown = hover || value;
  return (
    <span
      className={`star-rating star-rating--input ${className}`}
      role="group"
      aria-label={label ?? 'Your rating'}
      onMouseLeave={() => setHover(0)}
    >
      {Array.from({ length: max }, (_, i) => {
        const starValue = i + 1;
        const filled = starValue <= shown;
        return (
          <button
            key={starValue}
            type="button"
            role="radio"
            aria-checked={value === starValue}
            aria-label={`${starValue} star${starValue > 1 ? 's' : ''}`}
            className="star-rating-btn"
            onMouseEnter={() => setHover(starValue)}
            onFocus={() => setHover(starValue)}
            onBlur={() => setHover(0)}
            onClick={() => onChange(starValue)}
          >
            <Star
              size={size}
              aria-hidden="true"
              fill={filled ? 'currentColor' : 'none'}
            />
          </button>
        );
      })}
    </span>
  );
}
