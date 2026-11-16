import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { writeSnapshot, readSnapshot, clearStoredSession, clearSnapshots, SNAP_V } from './snapshot';

const signIn = (id) => {
  localStorage.setItem('amphive_token', 'tok');
  localStorage.setItem('amphive_user', JSON.stringify({ id }));
};

describe('snapshot', () => {
  beforeEach(() => localStorage.clear());
  afterEach(() => vi.useRealTimers());

  it('round-trips data for the signed-in uid', () => {
    signIn(7);
    writeSnapshot('plugs', 7, [{ id: 1 }]);
    const snap = readSnapshot('plugs', 7);
    expect(snap.data).toEqual([{ id: 1 }]);
    expect(typeof snap.savedAt).toBe('number');
  });

  it('does not write after logout or for another user', () => {
    writeSnapshot('plugs', 7, [1]);
    expect(localStorage.getItem('amphive_snap:plugs')).toBeNull();
    signIn(8);
    writeSnapshot('plugs', 7, [1]);
    expect(localStorage.getItem('amphive_snap:plugs')).toBeNull();
  });

  it('no-ops on a null uid', () => {
    signIn(7);
    writeSnapshot('plugs', null, [1]);
    expect(localStorage.getItem('amphive_snap:plugs')).toBeNull();
    expect(readSnapshot('plugs', undefined)).toBeNull();
  });

  it('deletes on uid mismatch, expiry and schema mismatch', () => {
    signIn(7);
    writeSnapshot('a', 7, 1);
    expect(readSnapshot('a', 9)).toBeNull();
    expect(localStorage.getItem('amphive_snap:a')).toBeNull();

    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-01-01T00:00:00Z'));
    writeSnapshot('b', 7, 1);
    vi.setSystemTime(new Date('2026-01-08T00:00:01Z'));
    expect(readSnapshot('b', 7)).toBeNull();
    expect(localStorage.getItem('amphive_snap:b')).toBeNull();

    localStorage.setItem('amphive_snap:c', JSON.stringify({ v: SNAP_V + 1, uid: 7, savedAt: Date.now(), data: 1 }));
    expect(readSnapshot('c', 7)).toBeNull();
    expect(localStorage.getItem('amphive_snap:c')).toBeNull();
  });

  it('clearStoredSession removes token, user and every snapshot', () => {
    signIn(7);
    ['plugs', 'upnext', 'stats', 'sessions'].forEach((k) => writeSnapshot(k, 7, 1));
    localStorage.setItem('amphive_config', '{}');
    clearStoredSession();
    expect(localStorage.getItem('amphive_token')).toBeNull();
    expect(localStorage.getItem('amphive_user')).toBeNull();
    expect(Object.keys(localStorage).filter((k) => k.startsWith('amphive_snap:'))).toEqual([]);
    // Public config survives.
    expect(localStorage.getItem('amphive_config')).toBe('{}');
  });

  it('clearSnapshots removes adjacent keys without skipping', () => {
    for (let i = 0; i < 5; i += 1) localStorage.setItem(`amphive_snap:k${i}`, 'x');
    clearSnapshots();
    expect(localStorage.length).toBe(0);
  });
});
