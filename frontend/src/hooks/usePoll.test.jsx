/**
 * usePoll tests: fires immediately, ticks on the interval, pauses while the
 * document is hidden (catching up on visibilitychange), and cleans up its
 * interval on unmount.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act } from '@testing-library/react';

import usePoll from './usePoll';

// jsdom's document.hidden is read-only — make it stubbable per test.
let hidden = false;
beforeEach(() => {
  hidden = false;
  vi.useFakeTimers();
  Object.defineProperty(document, 'hidden', {
    configurable: true,
    get: () => hidden,
  });
});

afterEach(() => {
  vi.useRealTimers();
  delete document.hidden;
});

describe('usePoll', () => {
  it('runs immediately and then on every interval', () => {
    const fn = vi.fn();
    renderHook(() => usePoll(fn, 1000));
    expect(fn).toHaveBeenCalledTimes(1);

    act(() => {
      vi.advanceTimersByTime(3000);
    });
    expect(fn).toHaveBeenCalledTimes(4);
  });

  it('pauses while the tab is hidden and catches up when visible again', () => {
    const fn = vi.fn();
    renderHook(() => usePoll(fn, 1000));
    expect(fn).toHaveBeenCalledTimes(1);

    hidden = true;
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(fn).toHaveBeenCalledTimes(1); // no ticks while hidden

    hidden = false;
    act(() => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(fn).toHaveBeenCalledTimes(2); // immediate catch-up tick
  });

  it('stops polling after unmount', () => {
    const fn = vi.fn();
    const { unmount } = renderHook(() => usePoll(fn, 1000));
    unmount();

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(fn).toHaveBeenCalledTimes(1); // only the initial call
  });

  it('restarts (with an immediate call) when deps change', () => {
    const fn = vi.fn();
    const { rerender } = renderHook(({ dep }) => usePoll(fn, 1000, [dep]), {
      initialProps: { dep: 'a' },
    });
    expect(fn).toHaveBeenCalledTimes(1);

    rerender({ dep: 'b' });
    expect(fn).toHaveBeenCalledTimes(2);
  });
});
