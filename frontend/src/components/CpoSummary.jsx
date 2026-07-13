/**
 * CpoSummary — shared metric strip for the CPO pages.
 * ====================================================
 * Replaces the inline "glass + flex + gap" summary bars that were copy-pasted
 * across the Dashboard, Gateways, and Sessions pages. Pass an array of
 * { label, value, tone? }; tone is 'accent' (amber — coins/revenue) or
 * 'success' (lime — online/health). Figures render mono + tabular so they line
 * up, matching the driver-side telemetry treatment.
 */

const CpoSummary = ({ items }) => (
  <div className="cpo-summary">
    {items.map((item, i) => (
      <div className="cpo-summary-item" key={item.label || i}>
        <span className="label">{item.label}</span>
        <span className={`value${item.tone ? ` ${item.tone}` : ''}`}>{item.value}</span>
      </div>
    ))}
  </div>
);

export default CpoSummary;
