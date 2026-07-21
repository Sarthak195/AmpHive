/**
 * EmptyState — true zero-data only (never for load failures; that's
 * ErrorState). Renders a .state-block with an optional lucide icon, a title,
 * supporting copy and an optional action element.
 */

export default function EmptyState({ icon: Icon, title, body, action }) {
  return (
    <div className="state-block">
      {Icon && <Icon className="state-icon" aria-hidden="true" />}
      <h3>{title}</h3>
      {body && <p>{body}</p>}
      {action}
    </div>
  );
}
