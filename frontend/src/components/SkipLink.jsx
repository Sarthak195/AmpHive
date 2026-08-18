/**
 * SkipLink — "Skip to main content", the first focusable element on the page.
 * ===========================================================================
 * Without it a keyboard or switch user tabs through the entire AppBar (or the
 * console sidebar's 14+ navigation links) on EVERY page load before reaching
 * anything they came for. It is one of the cheapest, highest-impact
 * accessibility affordances there is, and the app had none.
 *
 * Visually hidden until focused (see .skip-link in styles/base.css), so it
 * costs the visual design nothing.
 *
 * Target: every page renders its content inside a <main> element, so the link
 * moves focus to the first one rather than relying on an id that a page could
 * forget to set. `tabIndex = -1` is applied programmatically and removed on
 * blur so the landmark stays out of the normal tab order.
 */

export default function SkipLink() {
  const focusMain = (event) => {
    event.preventDefault();
    const main = document.querySelector('main');
    if (!main) return;
    main.setAttribute('tabindex', '-1');
    main.focus({ preventScroll: false });
    main.addEventListener('blur', () => main.removeAttribute('tabindex'), { once: true });
  };

  return (
    <a className="skip-link" href="#main-content" onClick={focusMain}>
      Skip to main content
    </a>
  );
}
