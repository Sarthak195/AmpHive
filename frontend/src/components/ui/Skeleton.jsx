/**
 * Skeleton — shimmer placeholders while data loads (.skeleton from
 * primitives.css). <Skeleton lines={3} /> renders n text lines;
 * <SkeletonTitle /> a heading-width bar. Both aria-hidden: loading is
 * announced by the surrounding container, not the shimmer.
 */

export default function Skeleton({ lines = 3 }) {
  return (
    <div className="stack-sm" aria-hidden="true">
      {Array.from({ length: lines }, (_, i) => (
        <div key={i} className="skeleton skeleton-text" />
      ))}
    </div>
  );
}

export function SkeletonTitle() {
  return <div className="skeleton skeleton-title" aria-hidden="true" />;
}
