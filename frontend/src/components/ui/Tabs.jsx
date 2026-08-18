/**
 * Tabs — accessible tab strip (.tabs/.tab from primitives.css).
 * tabs = [{ id, label, count? }]; counts render as .count-pill badges.
 * Roving tabindex + Arrow/Home/End keyboard navigation per the WAI-ARIA
 * tabs pattern; selection follows focus via onChange.
 *
 * Pass the active tab's content as `children` and Tabs renders the
 * role="tabpanel" wrapper itself. That is deliberate: the panel used to be a
 * plain sibling <div> owned by each page, so the tablist was an orphan —
 * assistive tech announced "tab 2 of 3" with no panel to move into, and there
 * was no way for a call site to reference the useId-generated tab ids. Owning
 * both halves here makes the wiring impossible to forget.
 *
 *   className      extra classes on the tablist (page-level margins etc.)
 *   panelClassName classes on the tabpanel wrapper
 *
 * Omitting `children` renders the bare strip (no panel) — only for strips
 * that genuinely control nothing addressable.
 */

import { useId, useRef } from 'react';

export default function Tabs({
  tabs,
  active,
  onChange,
  ariaLabel,
  children,
  className,
  panelClassName,
}) {
  const btnRefs = useRef({});
  // useId, not a hand-written prefix: two tab strips can share a page and
  // duplicate ids would silently point aria-controls at the wrong panel.
  const uid = useId();
  const tabId = (id) => `${uid}-tab-${id}`;
  const panelId = `${uid}-panel`;

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

  const tablist = (
    <div className={className ? `tabs ${className}` : 'tabs'} role="tablist" aria-label={ariaLabel}>
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
            id={tabId(t.id)}
            className="tab"
            aria-selected={selected}
            // Only the selected tab points at a panel: this component mounts
            // one panel at a time, so aria-controls on the inactive tabs would
            // be a dangling IDREF rather than a useful hint.
            aria-controls={selected ? panelId : undefined}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(t.id)}
            // On the button, not the tablist: with a roving tabindex only a
            // tab is ever focused, and a keydown handler on the container
            // would be asking a non-focusable element to service the keyboard.
            onKeyDown={handleKeyDown}
          >
            {t.label}
            {t.count != null && <> <span className="count-pill">{t.count}</span></>}
          </button>
        );
      })}
    </div>
  );

  if (children === undefined) return tablist;

  return (
    <>
      {tablist}
      {/* tabIndex=0 so the panel is reachable with one Tab press after
          arrow-keying the strip, and because some panels (empty states)
          contain nothing focusable of their own. */}
      <div
        id={panelId}
        className={panelClassName}
        role="tabpanel"
        aria-labelledby={tabId(active)}
        tabIndex={0}
      >
        {children}
      </div>
    </>
  );
}
