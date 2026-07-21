/**
 * TenantContext tests: the provider exposes the profile + badge counts from
 * the four console fetches, polls counts but fetches the profile only once,
 * treats every failure as non-fatal (previous values kept, no crash), copes
 * with both bare-array and {total,items} list shapes, and useTenant returns
 * an inert default outside a provider.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import { TenantProvider, useTenant } from './TenantContext';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

const PROFILE = {
  user: { id: 1, email: 'op@amphive.test', role: 'cpo' },
  tenant: { id: 7, name: 'Volt Yard' },
  stats: { gateway_count: 2, plug_count: 5, group_count: 1 },
};

const Probe = () => {
  const { profile, counts, loading, refresh } = useTenant();
  return (
    <div>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="org">{profile?.tenant?.name ?? 'none'}</span>
      <span data-testid="events">{String(counts.unackedEvents)}</span>
      <span data-testid="disputes">{String(counts.openDisputes)}</span>
      <span data-testid="capacity">{String(counts.pendingCapacity)}</span>
      <button onClick={() => refresh()}>refresh</button>
    </div>
  );
};

const mockRoutes = ({ profile, events, disputes, groups }) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/profile')) return profile();
    if (url.startsWith('/api/cpo/events')) return events();
    if (url.startsWith('/api/cpo/disputes')) return disputes();
    if (url.startsWith('/api/cpo/groups')) return groups();
    return Promise.reject(new Error(`unexpected ${url}`));
  });
};

const renderProvider = () =>
  render(
    <TenantProvider>
      <Probe />
    </TenantProvider>
  );

beforeEach(() => vi.clearAllMocks());

describe('TenantProvider', () => {
  it('exposes the profile and derives the three badge counts', async () => {
    mockRoutes({
      profile: () => Promise.resolve(PROFILE),
      events: () => Promise.resolve([{ id: 1 }, { id: 2 }, { id: 3 }]),
      disputes: () => Promise.resolve([{ id: 9 }]),
      groups: () =>
        Promise.resolve([
          { id: 1, pending_capacity_requests: 2 },
          { id: 2, pending_capacity_requests: 0 },
          { id: 3, pending_capacity_requests: 1 },
        ]),
    });

    renderProvider();
    expect(screen.getByTestId('loading')).toHaveTextContent('true');

    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));
    expect(screen.getByTestId('org')).toHaveTextContent('Volt Yard');
    expect(screen.getByTestId('events')).toHaveTextContent('3');
    expect(screen.getByTestId('disputes')).toHaveTextContent('1');
    expect(screen.getByTestId('capacity')).toHaveTextContent('3');

    // The right endpoints were hit (unacked events + open disputes).
    expect(api.get).toHaveBeenCalledWith(
      expect.stringContaining('/api/cpo/events?unacknowledged_only=true')
    );
    expect(api.get).toHaveBeenCalledWith('/api/cpo/disputes?status_filter=open');
  });

  it('understands paginated {total, items} list shapes', async () => {
    mockRoutes({
      profile: () => Promise.resolve(PROFILE),
      events: () => Promise.resolve({ total: 42, items: [{ id: 1 }] }),
      disputes: () => Promise.resolve({ total: 7, items: [] }),
      groups: () => Promise.resolve({ total: 1, items: [{ id: 1, pending_capacity_requests: 5 }] }),
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('events')).toHaveTextContent('42'));
    expect(screen.getByTestId('disputes')).toHaveTextContent('7');
    expect(screen.getByTestId('capacity')).toHaveTextContent('5');
  });

  it('treats every failure as non-fatal: null counts, no crash, loading settles', async () => {
    mockRoutes({
      profile: () => Promise.reject(new Error('boom')),
      events: () => Promise.reject(new Error('boom')),
      disputes: () => Promise.reject(new Error('boom')),
      groups: () => Promise.reject(new Error('boom')),
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'));
    expect(screen.getByTestId('org')).toHaveTextContent('none');
    expect(screen.getByTestId('events')).toHaveTextContent('null');
    expect(screen.getByTestId('disputes')).toHaveTextContent('null');
    expect(screen.getByTestId('capacity')).toHaveTextContent('null');
  });

  it('refresh() re-pulls counts (keeping last-good values on failure) but fetches the profile once', async () => {
    let eventsCall = 0;
    mockRoutes({
      profile: () => Promise.resolve(PROFILE),
      events: () => {
        eventsCall += 1;
        return eventsCall === 1
          ? Promise.resolve([{ id: 1 }, { id: 2 }])
          : Promise.reject(new Error('flaky'));
      },
      disputes: () => Promise.resolve([]),
      groups: () => Promise.resolve([]),
    });

    renderProvider();
    await waitFor(() => expect(screen.getByTestId('events')).toHaveTextContent('2'));

    await userEvent.click(screen.getByRole('button', { name: 'refresh' }));
    await waitFor(() => expect(eventsCall).toBe(2));

    // Failed refetch keeps the previous count; profile fetched exactly once.
    expect(screen.getByTestId('events')).toHaveTextContent('2');
    const profileCalls = api.get.mock.calls.filter(([url]) => url.startsWith('/api/cpo/profile'));
    expect(profileCalls).toHaveLength(1);
  });
});

describe('useTenant outside a provider', () => {
  it('returns an inert null-safe default', () => {
    render(<Probe />);
    expect(screen.getByTestId('org')).toHaveTextContent('none');
    expect(screen.getByTestId('events')).toHaveTextContent('null');
    expect(screen.getByTestId('loading')).toHaveTextContent('true');
  });
});
