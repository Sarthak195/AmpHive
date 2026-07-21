/**
 * TenantContext — the CPO console's shared tenant state.
 * =======================================================
 * Mounted once by CpoLayout so the 13 console pages stop fetching
 * `/api/cpo/profile` themselves or passing tenantName props around.
 *
 *   useTenant() -> {
 *     profile,                       // GET /api/cpo/profile response (or null)
 *     counts: {
 *       unackedEvents,               // unacknowledged gateway events (Health badge)
 *       openDisputes,                // open disputes (Disputes badge)
 *       pendingCapacity,             // drivers waiting on capacity (Groups badge)
 *     },
 *     loading,                       // true until the first fetch settles
 *     refresh,                       // re-pull counts now (e.g. after an ack)
 *   }
 *
 * Counts poll every 60s (usePoll — paused while the tab is hidden). Every
 * fetch failure is non-fatal: a failed count keeps its previous value (null
 * before the first success), so consumers must treat counts as nullable.
 */

import { createContext, useCallback, useContext, useRef, useState } from 'react';
import api from '../api/client';
import usePoll from '../hooks/usePoll';

const EMPTY_COUNTS = { unackedEvents: null, openDisputes: null, pendingCapacity: null };

const TenantContext = createContext(null);

/** Length of a list response that may be a bare array today or a paginated
    `{ total, items }` envelope after the backend catches up. */
const listCount = (res) => {
  if (Array.isArray(res)) return res.length;
  if (res && typeof res.total === 'number') return res.total;
  if (res && Array.isArray(res.items)) return res.items.length;
  return null;
};

const listItems = (res) => {
  if (Array.isArray(res)) return res;
  if (res && Array.isArray(res.items)) return res.items;
  return [];
};

export function TenantProvider({ children }) {
  const [profile, setProfile] = useState(null);
  const [counts, setCounts] = useState(EMPTY_COUNTS);
  const [loading, setLoading] = useState(true);
  // The profile is fetched once; only the badge counts re-poll.
  const profileLoaded = useRef(false);

  const refresh = useCallback(async () => {
    const wantProfile = !profileLoaded.current;
    const [profileRes, eventsRes, disputesRes, groupsRes] = await Promise.allSettled([
      wantProfile ? api.get('/api/cpo/profile') : Promise.resolve(null),
      api.get('/api/cpo/events?unacknowledged_only=true&limit=100'),
      api.get('/api/cpo/disputes?status_filter=open'),
      api.get('/api/cpo/groups'),
    ]);

    if (wantProfile && profileRes.status === 'fulfilled' && profileRes.value) {
      profileLoaded.current = true;
      setProfile(profileRes.value);
    }

    setCounts((prev) => ({
      unackedEvents:
        eventsRes.status === 'fulfilled' ? listCount(eventsRes.value) : prev.unackedEvents,
      openDisputes:
        disputesRes.status === 'fulfilled' ? listCount(disputesRes.value) : prev.openDisputes,
      pendingCapacity:
        groupsRes.status === 'fulfilled'
          ? listItems(groupsRes.value).reduce(
              (sum, g) => sum + (g.pending_capacity_requests || 0),
              0,
            )
          : prev.pendingCapacity,
    }));
    setLoading(false);
  }, []);

  usePoll(refresh, 60_000);

  return (
    <TenantContext.Provider value={{ profile, counts, loading, refresh }}>
      {children}
    </TenantContext.Provider>
  );
}

/** Null-safe accessor — components rendered outside a TenantProvider (unit
    tests, storybook-style harnesses) get an inert default instead of a crash. */
export function useTenant() {
  return (
    useContext(TenantContext) || {
      profile: null,
      counts: EMPTY_COUNTS,
      loading: true,
      refresh: () => {},
    }
  );
}

export default TenantContext;
