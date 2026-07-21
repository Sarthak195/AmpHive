/**
 * usePoll(fn, ms, deps) — the uniform polling idiom for live data.
 * Runs `fn` immediately, then every `ms` milliseconds — but only while the
 * tab is visible (`document.hidden` pauses ticks; becoming visible again
 * fires one immediately). Cleans up its interval + listener on unmount or
 * when `ms`/`deps` change. `fn` is kept in a ref so callers don't need to
 * memoize it.
 */

import { useEffect, useRef } from 'react';

export default function usePoll(fn, ms, deps = []) {
  const fnRef = useRef(fn);
  useEffect(() => {
    fnRef.current = fn;
  });

  useEffect(() => {
    const tick = () => {
      if (!document.hidden) fnRef.current();
    };
    tick();
    const id = setInterval(tick, ms);
    const onVisibility = () => {
      if (!document.hidden) tick();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ms, ...deps]);
}
