/**
 * CpoFaults tests: the fault console renders fixture events/plugs, gates the
 * maintenance action to plug-scoped safety faults only (not UNAUTHORIZED_ON),
 * always shows the "in maintenance" top strip, and drives acknowledge /
 * enter-maintenance through the API client.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoFaults from './CpoFaults';
import api from '../../api/client';
import { useSession } from '../../contexts/SessionContext';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock('../../contexts/SessionContext', () => ({
  useSession: vi.fn(),
}));
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'cpo@amphive.test', full_name: 'Ops' } }),
}));

const EVENTS = [
  {
    id: 1, gateway_id: 'gw-1', plug_id: 5, event_type: 'THERMAL_CUTOFF',
    severity: 'critical', detail: 'Plug reported overheat — session cut off locally.',
    acknowledged: false, created_at: new Date(Date.now() - 60000).toISOString(),
  },
  {
    id: 2, gateway_id: 'gw-1', plug_id: 9, event_type: 'UNAUTHORIZED_ON',
    severity: 'critical', detail: 'Plug switched ON with no active session.',
    acknowledged: false, created_at: new Date(Date.now() - 120000).toISOString(),
  },
];

// Plug 5 is already in maintenance (matches event #1's THERMAL_CUTOFF);
// plug 9 is available (event #2 is UNAUTHORIZED_ON, which never offers a
// maintenance action regardless of plug status).
const PLUGS = [
  { id: 5, name: 'Bay 1', gateway_id: 'gw-1', status: 'maintenance' },
  { id: 9, name: 'Bay 2', gateway_id: 'gw-1', status: 'available' },
];

const PROFILE = { tenant: { name: 'Acme Charging' } };

const mockApiGet = (events = EVENTS, plugs = PLUGS, profile = PROFILE) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/events')) return Promise.resolve(events);
    if (url === '/api/cpo/plugs') return Promise.resolve(plugs);
    if (url === '/api/cpo/profile') return Promise.resolve(profile);
    return Promise.resolve([]);
  });
};

const renderPage = () => render(
  <MemoryRouter>
    <CpoFaults />
  </MemoryRouter>
);

beforeEach(() => {
  vi.clearAllMocks();
  useSession.mockReturnValue({ alarms: [] });
});

describe('CpoFaults', () => {
  it('renders fixture events with severity, gateway, and humanized event type', async () => {
    mockApiGet();
    renderPage();

    expect(await screen.findByText('THERMAL CUTOFF')).toBeInTheDocument();
    expect(screen.getByText('UNAUTHORIZED ON')).toBeInTheDocument();
    expect(screen.getAllByText('gw-1').length).toBe(2); // one per event row
    expect(screen.getAllByText('critical').length).toBe(2); // severity badges
  });

  it('offers a maintenance action only for a plug-scoped safety fault, not UNAUTHORIZED_ON', async () => {
    mockApiGet();
    renderPage();
    await screen.findByText('THERMAL CUTOFF');

    const rows = screen.getAllByRole('row');
    const unauthorizedRow = rows.find((r) => r.textContent.includes('UNAUTHORIZED ON'));
    expect(within(unauthorizedRow).queryByText(/maintenance/i)).not.toBeInTheDocument();

    // Event #1 (THERMAL_CUTOFF on plug 5, already in maintenance) offers "Clear maintenance".
    const thermalRow = rows.find((r) => r.textContent.includes('THERMAL CUTOFF'));
    expect(within(thermalRow).getByRole('button', { name: 'Clear maintenance' })).toBeInTheDocument();
  });

  it('shows plugs currently in maintenance in the top strip', async () => {
    mockApiGet();
    renderPage();
    const heading = await screen.findByText(/In Maintenance/);
    const strip = heading.closest('.glass');

    // Plug 5 (maintenance) is in the strip; plug 9 (available) is not — even
    // though "Bay 2" legitimately appears elsewhere, in the events table.
    expect(within(strip).getByText('Bay 1')).toBeInTheDocument();
    expect(within(strip).queryByText('Bay 2')).not.toBeInTheDocument();
  });

  it('acknowledges an event and removes it from the list', async () => {
    mockApiGet();
    api.post.mockResolvedValue({ status: 'acknowledged', event_id: 2 });
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('UNAUTHORIZED ON');
    const rows = screen.getAllByRole('row');
    const unauthorizedRow = rows.find((r) => r.textContent.includes('UNAUTHORIZED ON'));
    await user.click(within(unauthorizedRow).getByRole('button', { name: 'Acknowledge' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/events/2/ack', {});
    await waitFor(() => expect(screen.queryByText('UNAUTHORIZED ON')).not.toBeInTheDocument());
  });

  it('puts a plug in maintenance from a critical fault row', async () => {
    const events = [{
      id: 3, gateway_id: 'gw-2', plug_id: 9, event_type: 'OVERCURRENT_CUTOFF',
      severity: 'critical', detail: 'over-current', acknowledged: false,
      created_at: new Date().toISOString(),
    }];
    mockApiGet(events, PLUGS);
    api.post.mockResolvedValue({ status: 'updated', plug_id: 9, action: 'enter', plug_status: 'maintenance' });
    const user = userEvent.setup();
    renderPage();

    const enterButton = await screen.findByRole('button', { name: 'Put in maintenance' });
    await user.click(enterButton);

    expect(api.post).toHaveBeenCalledWith('/api/cpo/plugs/9/maintenance', { action: 'enter' });
  });
});
