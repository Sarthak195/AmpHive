/**
 * Last-known data snapshots for the instant app open.
 * ===================================================
 * Personal data the app shows on open (charger list, up-next strip, month
 * stats, active session rows) is saved here from successful API responses so
 * a repeat open can paint it before any network round trip. Rules:
 * - Tagged with the user id and a schema version; a read for another user, an
 *   old schema or an entry older than 7 days (the sliding JWT window) deletes
 *   it and returns null.
 * - Written only while a token is stored and the cached user is this uid, so a
 *   fetch that resolves after logout or a 401 cannot write data back.
 * - clearStoredSession() is the one place that drops the token, the cached
 *   user and every snapshot (logout, logout everywhere, any 401).
 * Callers must mark anything rendered from a snapshot as not live.
 */

const PREFIX = 'amphive_snap:';
// Bump when the shape of any snapshotted payload changes, so a new bundle
// never renders an old shape (an old shape that crashes would repeat on every
// open until it expired).
export const SNAP_V = 1;
const TTL_MS = 7 * 24 * 60 * 60 * 1000;

const storedUid = () => {
  if (!localStorage.getItem('amphive_token')) return null;
  return JSON.parse(localStorage.getItem('amphive_user'))?.id ?? null;
};

export function writeSnapshot(key, uid, data) {
  try {
    if (uid == null || String(storedUid()) !== String(uid)) return;
    localStorage.setItem(PREFIX + key, JSON.stringify({ v: SNAP_V, uid, savedAt: Date.now(), data }));
  } catch {
    // Quota, blocked storage or an unreadable cached user: skip the snapshot.
  }
}

/** Returns { data, savedAt } or null. */
export function readSnapshot(key, uid) {
  if (uid == null) return null;
  try {
    const raw = localStorage.getItem(PREFIX + key);
    if (!raw) return null;
    const snap = JSON.parse(raw);
    const age = Date.now() - Number(snap?.savedAt);
    if (snap?.v === SNAP_V && String(snap.uid) === String(uid) && age >= 0 && age < TTL_MS) {
      return { data: snap.data, savedAt: snap.savedAt };
    }
    localStorage.removeItem(PREFIX + key);
  } catch {
    try {
      localStorage.removeItem(PREFIX + key);
    } catch {
      // Storage unavailable.
    }
  }
  return null;
}

export function clearSnapshots() {
  try {
    // Collect first: removing while iterating by index skips keys.
    const keys = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (k && k.startsWith(PREFIX)) keys.push(k);
    }
    keys.forEach((k) => localStorage.removeItem(k));
  } catch {
    // Storage unavailable.
  }
}

export function clearStoredSession() {
  try {
    localStorage.removeItem('amphive_token');
    localStorage.removeItem('amphive_user');
  } catch {
    // Storage unavailable.
  }
  clearSnapshots();
}

/** The cached user's id, for a login that may switch accounts. */
export function cachedUserId() {
  try {
    return JSON.parse(localStorage.getItem('amphive_user'))?.id ?? null;
  } catch {
    return null;
  }
}
