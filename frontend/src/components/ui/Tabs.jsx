/**
 * Tabs — accessible tab strip (.tabs/.tab from primitives.css).
 * tabs = [{ id, label, count? }]; counts render as .count-pill badges.
 * Roving tabindex + Arrow/Home/End keyboard navigation per the WAI-ARIA
 * tabs pattern; selection follows focus via onChange.
 */

import { useRef } from 'react';

export default function Tabs({ tabs, active, onChange, ariaLabel }) {
  const btnRefs = useRef({});

  const handleKeyDown = (e) => {
    const idx = tabs.findIndex((t) => t.id === active);
    let next = null;
    if (e.key === 'ArrowRight') next = tabs[(idx + 1) % tabs.length];
    else if (e.key === 'ArrowLeft') next = tabs[(idx - 1 + tabs.length) % tabs.length];
    else if (e.key === 'Home') next = tabs[0];
    else if (e.key === 'End') next = tabs[tabs.length - 1];
    if (next && next.id !== active) {
      e.preventDefault();
      onChange(next.id);
      btnRefs.current[next.id]?.focus();
    } else if (next) {
      e.preventDefault();
    }
  };

  return (
    <div className="tabs" role="tablist" aria-label={ariaLabel} onKeyDown={handleKeyDown}>
      {tabs.map((t) => {
        const selected = t.id === active;
        return (
          <button
            key={t.id}
            ref={(el) => {
              btnRefs.current[t.id] = el;
            }}
            type="button"
            role="tab"
            className="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.id)}
          >
            {t.label}
            {t.count != null && <> <span className="count-pill">{t.count}</span></>}
          </button>
        );
      })}
    </div>
  );
}
