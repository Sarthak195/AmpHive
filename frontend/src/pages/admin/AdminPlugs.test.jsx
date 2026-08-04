/**
 * AdminPlugs tests: the cross-tenant charger table renders from
 * GET /api/admin/plugs — name/tenant/gateway, the public/private visibility
 * badge, derived status dot, W→kW power, model/connector, and last-telemetry
 * recency. The visibility/status selects and the debounced search box each
 * change the query string sent to the mocked api.get and reset pagination.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import AdminPlugs from './AdminPlugs';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const PLUGS = {
  total: 2,
  items: [
    {
      id: 1,
      name: 'Bay 1',
      status: 'available',
      gateway_id: 'gw1',
      gateway_name: 'Main Hub',
      tenant_id: 't1',
      tenant_name: 'Acme Charging',
      group_id: null,
      group_name: null,
      visibility: 'public',
      local_ip: '192.168.1.10',
      plug_model: 'Tapo P110',
      connector_type: 'Type 2',
      rated_power_w: 3300,
      current_power_w: 2300,
      last_telemetry_at: new Date(Date.now() - 60_000).toISOString(),
      tariff_id: null,
    },
    {
      id: 2,
      name: 'Bay 2',
      status: 'occupied',
      gateway_id: 'gw1',
      gateway_name: 'Main Hub',
      tenant_id: 't2',
      tenant_name: 'EV Express',
      group_id: 5,
      group_name: 'Fleet',
      visibility: 'private',
      local_ip: '192.168.1.11',
      plug_model: 'Tapo P110',
      connector_type: '3-pin 16A',
      rated_power_w: 3300,
      current_power_w: 0,
      last_telemetry_at: null,
      tariff_id: 7,
    },
  ],
};

const renderPage = () => render(<MemoryRouter><AdminPlugs /></MemoryRouter>);

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(PLUGS);
});
afterEach(() => vi.restoreAllMocks());

describe('AdminPlugs', () => {
  it('renders rows with tenant, gateway, visibility badges, status, power and connector', async () => {
    renderPage();

    expect(await screen.findByText('Bay 1')).toBeInTheDocument();
    expect(screen.getByText('Bay 2')).toBeInTheDocument();

    // Tenant + gateway
    expect(screen.getByText('Acme Charging')).toBeInTheDocument();
    expect(screen.getByText('EV Express')).toBeInTheDocument();
    expect(screen.getAllByText('Main Hub')).toHaveLength(2);

    // Visibility badges (scoped past the filter <option>s of the same text)
    expect(screen.getByText('Public', { selector: 'span.badge' })).toBeInTheDocument();
    expect(screen.getByText('Private', { selector: 'span.badge' })).toBeInTheDocument();

    // Derived status labels (occupied → "In use")
    expect(screen.getByText('Available', { selector: '.status-dot-label' })).toBeInTheDocument();
    expect(screen.getByText('In use', { selector: '.status-dot-label' })).toBeInTheDocument();

    // Power formatting: 2300 W → 2.3 kW, 0 W → "0 W"
    expect(screen.getByText('2.3 kW')).toBeInTheDocument();
    expect(screen.getByText('0 W')).toBeInTheDocument();

    // Connector types
    expect(screen.getByText('Type 2')).toBeInTheDocument();
    expect(screen.getByText('3-pin 16A')).toBeInTheDocument();

    // Header count
    expect(screen.getByText('2 chargers')).toBeInTheDocument();
  });

  it('loads the first page with limit/offset on mount', async () => {
    renderPage();
    await screen.findByText('Bay 1');
    expect(api.get).toHaveBeenCalledWith('/api/admin/plugs?limit=25&offset=0');
  });

  it('changes the query string when the visibility filter changes', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.selectOptions(screen.getByLabelText('Filter by visibility'), 'public');
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('visibility=public'));
  });

  it('changes the query string when the status filter changes', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.selectOptions(screen.getByLabelText('Filter by status'), 'occupied');
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('status=occupied'));
  });

  it('debounces the search box into a q= query param', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.type(screen.getByPlaceholderText('Search by name'), 'Bay');
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith(expect.stringContaining('q=Bay'))
    );
  });

  it('shows an empty state when no chargers exist', async () => {
    api.get.mockResolvedValue({ total: 0, items: [] });
    renderPage();
    expect(await screen.findByText('No chargers yet')).toBeInTheDocument();
  });

  it('surfaces a retryable ErrorState on fetch failure', async () => {
    api.get.mockRejectedValueOnce(new Error('Network down'));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    api.get.mockResolvedValueOnce(PLUGS);
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Bay 1')).toBeInTheDocument();
  });
});
