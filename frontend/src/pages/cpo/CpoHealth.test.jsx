/**
 * CpoHealth tests (redesign v3, D3): the events table (severity badge, event
 * copy, gateway/plug name resolution, unacknowledged-only default), single +
 * bulk Acknowledge, the always-visible in-maintenance strip, the
 * enter/clear-maintenance row actions for hardware safety faults, and the
 * debounced (>2s) refetch on a live `gateway_alarm` socket signal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoHealth from './CpoHealth';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div data-testid="cpo-layout">{children}</div>,
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const sessionState = vi.hoisted(() => ({ alarms: [] }));
vi.mock('../../contexts/SessionContext', () => ({
  useSession: () => sessionState,
}));

const refreshTenant = vi.fn();
vi.mock('../../contexts/TenantContext', () => ({
  useTenant: () => ({ refresh: refreshTenant }),
}));

const EVENTS = {
  total: 2,
  items: [
    {
      id: 1,
      gateway_id: 'gw-1',
      plug_id: 10,
      event_type: 'OVERCURRENT_CUTOFF',
      severity: 'critical',
      detail: 'Drew 22A on a 16A cap',
      acknowledged: false,
      created_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    },
    {
      id: 2,
      gateway_id: 'gw-1',
      plug_id: null,
      event_type: 'OTA_OK_REBOOTING',
      severity: 'info',
      detail: null,
      acknowledged: false,
      created_at: new Date(Date.now() - 60 * 60_000).toISOString(),
    },
  ],
};

const PLUGS = [
  { id: 10, name: 'Garage plug', gateway_id: 'gw-1', status: 'available' },
  { id: 11, name: 'Porch plug', gateway_id: 'gw-1', status: 'maintenance' },
];

const GATEWAYS = [{ id: 'gw-1', name: 'Main gateway' }];

const mockApiRoutes = ({ events = EVENTS, plugs = PLUGS, gateways = GATEWAYS } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/events')) return Promise.resolve(events);
    if (url === '/api/cpo/plugs') return Promise.resolve(plugs);
    if (url === '/api/cpo/gateways') return Promise.resolve(gateways);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = () => render(<CpoHealth />);

beforeEach(() => {
  vi.clearAllMocks();
  sessionState.alarms = [];
  mockApiRoutes();
});

describe('CpoHealth', () => {
  it('fetches with unacknowledged_only=true by default and renders resolved names', async () => {
    renderPage();

    expect(await screen.findByText('Current safety cutoff')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith(
      expect.stringContaining('unacknowledged_only=true')
    );
    expect(screen.getAllByText('Main gateway').length).toBe(2);
    expect(screen.getByText('Garage plug')).toBeInTheDocument();
    expect(screen.getByText('critical')).toBeInTheDocument();
    expect(screen.getByText('Drew 22A on a 16A cap')).toBeInTheDocument();
  });

  it('shows the in-maintenance strip independent of the event filters', async () => {
    renderPage();
    await screen.findByText('Current safety cutoff');

    expect(screen.getByText('In maintenance (1)')).toBeInTheDocument();
    expect(screen.getByText('Porch plug')).toBeInTheDocument();
  });

  it('clears maintenance from the top strip', async () => {
    api.post.mockResolvedValue({ status: 'updated' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Porch plug');

    const strip = screen.getByText('In maintenance (1)').closest('.well');
    await user.click(within(strip).getByRole('button', { name: 'Clear' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/plugs/11/maintenance', { action: 'clear' });
  });

  it('offers "Put in maintenance" on a safety-fault row for a plug not yet in maintenance', async () => {
    api.post.mockResolvedValue({ status: 'updated' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Current safety cutoff');

    await user.click(screen.getByRole('button', { name: 'Put in maintenance' }));
    expect(api.post).toHaveBeenCalledWith('/api/cpo/plugs/10/maintenance', { action: 'enter' });
  });

  it('acknowledges a single event and refreshes the tenant badge count', async () => {
    api.post.mockResolvedValue({ status: 'acknowledged' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Current safety cutoff');

    const row = screen.getByText('Current safety cutoff').closest('tr');
    await user.click(within(row).getByRole('button', { name: 'Acknowledge' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/events/1/ack', {});
    await waitFor(() => expect(refreshTenant).toHaveBeenCalled());
  });

  it('selects events and bulk-acknowledges them', async () => {
    api.post.mockResolvedValue({ status: 'acknowledged' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Current safety cutoff');

    const checkboxes = screen.getAllByRole('checkbox').filter((c) => c.getAttribute('aria-label')?.startsWith('Select event'));
    expect(checkboxes).toHaveLength(2);
    await user.click(checkboxes[0]);
    await user.click(checkboxes[1]);

    expect(await screen.findByText('2 selected')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Acknowledge selected' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/events/1/ack', {});
    expect(api.post).toHaveBeenCalledWith('/api/cpo/events/2/ack', {});
    await waitFor(() => expect(toast.ok).toHaveBeenCalled());
  });

  it('toggles the unacknowledged-only filter and severity filter', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Current safety cutoff');

    await user.click(screen.getByLabelText('Unacknowledged only'));
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('unacknowledged_only=true'));
    // Latest call should now omit the unacknowledged_only param
    const lastEventsCall = api.get.mock.calls.filter(([u]) => u.startsWith('/api/cpo/events')).pop();
    expect(lastEventsCall[0]).not.toContain('unacknowledged_only');

    await user.selectOptions(screen.getByLabelText('Severity'), 'critical');
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('severity=critical'));
  });

  it('shows a retryable ErrorState on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/events')) return Promise.reject(new Error('Network down'));
      if (url === '/api/cpo/plugs') return Promise.resolve(PLUGS);
      if (url === '/api/cpo/gateways') return Promise.resolve(GATEWAYS);
      return Promise.reject(new Error('unhandled'));
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    mockApiRoutes();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('Current safety cutoff');
  });

  it('shows an empty state (not an error) when there are no unacknowledged events', async () => {
    mockApiRoutes({ events: { total: 0, items: [] } });
    renderPage();

    expect(await screen.findByText('No unacknowledged events')).toBeInTheDocument();
  });

  it('refetches once a live gateway_alarm signal arrives, and coalesces a fast second one', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { rerender } = render(<CpoHealth />);
    await vi.waitFor(() => expect(api.get).toHaveBeenCalled());
    const countOf = () => api.get.mock.calls.filter(([u]) => u.startsWith('/api/cpo/events')).length;
    const callsBefore = countOf();

    // First alarm arrives well after the initial load — refetches immediately.
    await vi.advanceTimersByTimeAsync(3000);
    sessionState.alarms = [{ id: 'a1' }];
    rerender(<CpoHealth />);
    await vi.waitFor(() => expect(countOf()).toBe(callsBefore + 1));

    // A second alarm arriving <2s later is coalesced into one debounced
    // refetch instead of firing immediately again.
    sessionState.alarms = [{ id: 'a1' }, { id: 'a2' }];
    rerender(<CpoHealth />);
    expect(countOf()).toBe(callsBefore + 1);

    await vi.advanceTimersByTimeAsync(2100);
    expect(countOf()).toBe(callsBefore + 2);

    vi.useRealTimers();
  });
});
