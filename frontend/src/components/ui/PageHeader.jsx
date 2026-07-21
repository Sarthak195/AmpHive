/**
 * PageHeader — the one h1 per page (.page-header from primitives.css):
 * optional uppercase eyebrow, the title, optional supporting line and an
 * actions cluster on the right.
 */

export default function PageHeader({ eyebrow, title, sub, actions }) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <div className="page-eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {sub && <p>{sub}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  );
}
