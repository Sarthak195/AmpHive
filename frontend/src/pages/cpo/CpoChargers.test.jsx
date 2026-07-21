/**
 * CpoChargers tests: the fleet table renders effective status (gateway +
 * plug), gateway/group/tariff resolution and a best-effort "price now",
 * search/status/gateway/group filters (including the ?group= deep link from
 * Groups), Add/Edit charger, the per-row maintenance toggle, bulk actions
 * (assign to group, put in maintenance), and the QR deep-link modal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoChargers from './CpoChargers';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', () => ({
  useToast: () => toast,
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div>{children}</div>,
}));

let mockConfig = { coin_inr_rate: 1 };
vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => mockConfig,
}));

const PLUGS = [
  { id: 1, name: 'Bay 1', gateway_id: 'gw1', local_ip: '192.168.1.10', status: 'available', group_id: null, group_name: null, max_current_a: null, queued_charging_enabled: null, auto_start_delay_min: null },
  { id: 2, name: 'Bay 2', gateway_id: 'gw1', local_ip: '192.168.1.11', status: 'occupied', group_id: 5, group_name: 'Society A', tariff_id: 9, max_current_a: 10, queued_charging_enabled: true, auto_start_delay_min: 5 },
  { id: 3, name: 'Bay 3', gateway_id: 'gw2', local_ip: '192.168.1.12', status: 'available', group_id: null, group_name: null, max_current_a: null, queued_charging_enabled: null, auto_start_delay_min: null },
  { id: 4, name: 'Bay 4', gateway_id: 'gw1', local_ip: '192.168.1.13', status: 'maintenance', group_id: null, group_name: null, max_current_a: null, queued_charging_enabled: null, auto_start_delay_min: null },
];

const GATEWAYS = [
  { id: 'gw1', name: 'Gate A', status: 'online', firmware_version: '2.3.0', plug_count: 3 },
  { id: 'gw2', name: 'Gate B', status: 'offline', firmware_version: '2.1.0', plug_count: 1 },
];

const GROUPS = [
  { id: 5, name: 'Society A', is_public: false, plug_count: 1, member_count: 2, max_current_a: 32, current_load_a: 10, pending_capacity_requests: 0 },
];

const TARIFFS = [{ id: 9, name: 'Peak plan', price_per_kwh: 8 }];

const mockApi = (overrides = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/plugs') return Promise.resolve(overrides.plugs ?? PLUGS);
    if (url === '/api/cpo/gateways') return Promise.resolve(overrides.gateways ?? GATEWAYS);
    if (url === '/api/cpo/groups') return Promise.resolve(overrides.groups ?? GROUPS);
    if (url === '/api/cpo/tariffs') return Promise.resolve(overrides.tariffs ?? TARIFFS);
    if (url.startsWith('/api/plugs/') && url.endsWith('/tariff-preview')) {
      return Promise.resolve({ base_price_per_kwh: 8, price_now: 8, slots: [] });
    }
    return Promise.resolve([]);
  });
};

const renderPage = (initialEntries = ['/cpo/chargers']) =>
  render(
    <MemoryRouter initialEntries={initialEntries}>
      <CpoChargers />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  mockConfig = { coin_inr_rate: 1 };
});

describe('list', () => {
  it('renders effective status, resolved gateway/group/tariff, and price now', async () => {
    mockApi();
    renderPage();

    await screen.findByText('Bay 1');
    const rows = screen.getAllByRole('row');

    const row1 = rows.find((r) => within(r).queryByText('Bay 1'));
    expect(within(row1).getByText('Available')).toBeInTheDocument();
    expect(within(row1).getByText('Gate A')).toBeInTheDocument();
    expect(within(row1).getByText('Ungrouped')).toBeInTheDocument();
    expect(within(row1).getByText('Inherited')).toBeInTheDocument();

    const row2 = rows.find((r) => within(r).queryByText('Bay 2'));
    expect(within(row2).getByText('In use')).toBeInTheDocument();
    expect(within(row2).getByText('Society A')).toBeInTheDocument();
    expect(within(row2).getByText('Peak plan')).toBeInTheDocument();

    const row3 = rows.find((r) => within(r).queryByText('Bay 3'));
    expect(within(row3).getByText('Offline')).toBeInTheDocument(); // gateway unreachable

    const row4 = rows.find((r) => within(r).queryByText('Bay 4'));
    expect(within(row4).getByText('Under maintenance')).toBeInTheDocument();

    expect(screen.getByText('4 of 4 chargers')).toBeInTheDocument();

    await waitFor(() => expect(within(row2).getAllByText(/₹8\.00/).length).toBeGreaterThan(0));
  });

  it('shows a retryable ErrorState instead of a blank table on failure', async () => {
    api.get.mockRejectedValue(new Error('boom'));
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();

    mockApi();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Bay 1')).toBeInTheDocument();
  });

  it('shows the empty state when there are no chargers', async () => {
    mockApi({ plugs: [] });
    renderPage();

    expect(await screen.findByText('No chargers yet')).toBeInTheDocument();
  });
});

describe('filters', () => {
  it('filters by search text', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.type(screen.getByLabelText('Search chargers'), 'Bay 2');

    expect(screen.queryByText('Bay 1')).not.toBeInTheDocument();
    expect(screen.getByText('Bay 2')).toBeInTheDocument();
    expect(screen.getByText('1 of 4 chargers')).toBeInTheDocument();
  });

  it('filters by effective status', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.selectOptions(screen.getByLabelText('Filter by status'), 'maintenance');

    expect(screen.queryByText('Bay 1')).not.toBeInTheDocument();
    expect(screen.getByText('Bay 4')).toBeInTheDocument();
  });

  it('filters by gateway', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.selectOptions(screen.getByLabelText('Filter by gateway'), 'gw2');

    expect(screen.getByText('Bay 3')).toBeInTheDocument();
    expect(screen.queryByText('Bay 1')).not.toBeInTheDocument();
  });

  it('honors the ?group= deep link from the Groups page', async () => {
    mockApi();
    renderPage(['/cpo/chargers?group=5']);

    await screen.findByText('Bay 2');
    expect(screen.queryByText('Bay 1')).not.toBeInTheDocument();
    expect(screen.getByText('1 of 4 chargers')).toBeInTheDocument();
  });
});

describe('add charger', () => {
  it('requires gateway, name, and IP before submitting', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.click(screen.getByRole('button', { name: 'Add charger' }));
    const dialog = screen.getByRole('dialog', { name: 'Add charger' });
    await user.click(within(dialog).getByRole('button', { name: 'Add charger' }));

    expect(screen.getByText('Gateway, name, and IP address are required.')).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('submits the new charger and refetches', async () => {
    mockApi();
    api.post.mockResolvedValue({ status: 'registered', plug_id: 99 });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    await user.click(screen.getByRole('button', { name: 'Add charger' }));
    const dialog = screen.getByRole('dialog', { name: 'Add charger' });
    await user.selectOptions(within(dialog).getByLabelText('Gateway'), 'gw1');
    await user.type(within(dialog).getByLabelText('Charger name'), 'Bay 5');
    await user.type(within(dialog).getByLabelText('Local IP address'), '192.168.1.20');
    await user.click(within(dialog).getByRole('button', { name: 'Add charger' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/plugs', {
        gateway_id: 'gw1',
        name: 'Bay 5',
        local_ip: '192.168.1.20',
      })
    );
    expect(toast.ok).toHaveBeenCalled();
  });
});

describe('edit charger', () => {
  it('prefills the form and saves changed identity + pricing fields', async () => {
    mockApi();
    api.put.mockResolvedValue({ status: 'updated' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 2');

    const rows = screen.getAllByRole('row');
    const row2 = rows.find((r) => within(r).queryByText('Bay 2'));
    await user.click(within(row2).getByRole('button', { name: 'Edit' }));

    const dialog = screen.getByRole('dialog', { name: 'Edit Bay 2' });
    expect(within(dialog).getByLabelText('Charger name')).toHaveValue('Bay 2');
    expect(within(dialog).getByLabelText('Tariff')).toHaveValue('9');

    await user.clear(within(dialog).getByLabelText('Charger name'));
    await user.type(within(dialog).getByLabelText('Charger name'), 'Bay 2 renamed');
    await user.selectOptions(within(dialog).getByLabelText('Tariff'), '');

    await user.click(within(dialog).getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/plugs/2', { name: 'Bay 2 renamed' })
    );
    expect(api.put).toHaveBeenCalledWith('/api/cpo/plugs/2/tariff', { tariff_id: null });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('shows the sub-16A advisory banner in the Safety section', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    const rows = screen.getAllByRole('row');
    const row1 = rows.find((r) => within(r).queryByText('Bay 1'));
    await user.click(within(row1).getByRole('button', { name: 'Edit' }));

    expect(
      screen.getByText(/Below-16A limits are enforced when starting sessions/)
    ).toBeInTheDocument();
  });
});

describe('per-row maintenance toggle', () => {
  it('puts an available charger into maintenance', async () => {
    mockApi();
    api.post.mockResolvedValue({ status: 'updated', action: 'enter', plug_status: 'maintenance' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    const rows = screen.getAllByRole('row');
    const row1 = rows.find((r) => within(r).queryByText('Bay 1'));
    await user.click(within(row1).getByRole('button', { name: 'Put in maintenance' }));

    const dialog = screen.getByRole('dialog', { name: 'Put in maintenance' });
    await user.click(within(dialog).getByRole('button', { name: 'Put in maintenance' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/plugs/1/maintenance', { action: 'enter' })
    );
    expect(toast.ok).toHaveBeenCalled();
  });

  it('clears maintenance on a charger already in maintenance', async () => {
    mockApi();
    api.post.mockResolvedValue({ status: 'updated', action: 'clear', plug_status: 'available' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 4');

    const rows = screen.getAllByRole('row');
    const row4 = rows.find((r) => within(r).queryByText('Bay 4'));
    await user.click(within(row4).getByRole('button', { name: 'Clear maintenance' }));

    const dialog = screen.getByRole('dialog', { name: 'Clear maintenance' });
    await user.click(within(dialog).getByRole('button', { name: 'Clear maintenance' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/plugs/4/maintenance', { action: 'clear' })
    );
  });

  it('surfaces a 409 (active session) inline via a toast', async () => {
    mockApi();
    api.post.mockRejectedValue(new Error('Cannot clear maintenance: this plug has an active charging session.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 4');

    const rows = screen.getAllByRole('row');
    const row4 = rows.find((r) => within(r).queryByText('Bay 4'));
    await user.click(within(row4).getByRole('button', { name: 'Clear maintenance' }));
    const dialog = screen.getByRole('dialog', { name: 'Clear maintenance' });
    await user.click(within(dialog).getByRole('button', { name: 'Clear maintenance' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'Cannot clear maintenance: this plug has an active charging session.'
      )
    );
  });
});

describe('bulk actions', () => {
  it('assigns selected chargers to a group by looping the existing PUT', async () => {
    mockApi();
    api.put.mockResolvedValue({ status: 'updated' });
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText('Bay 1');

    await user.click(screen.getByLabelText('Select Bay 1'));
    await user.click(screen.getByLabelText('Select Bay 3'));

    const bulkBar = container.querySelector('.cpo-chargers-bulkbar');
    expect(within(bulkBar).getByText('2 selected')).toBeInTheDocument();
    await user.click(within(bulkBar).getByRole('button', { name: 'Assign to group' }));

    const dialog = screen.getByRole('dialog', { name: /Assign 2 chargers/ });
    await user.selectOptions(within(dialog).getByLabelText('Group'), '5');
    await user.click(within(dialog).getByRole('button', { name: 'Assign' }));

    await waitFor(() => {
      expect(api.put).toHaveBeenCalledWith('/api/cpo/plugs/1', { group_id: 5 });
      expect(api.put).toHaveBeenCalledWith('/api/cpo/plugs/3', { group_id: 5 });
    });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('puts selected chargers into maintenance by looping the existing PUT', async () => {
    mockApi();
    api.put.mockResolvedValue({ status: 'updated' });
    const user = userEvent.setup();
    const { container } = renderPage();
    await screen.findByText('Bay 1');

    await user.click(screen.getByLabelText('Select Bay 1'));

    const bulkBar = container.querySelector('.cpo-chargers-bulkbar');
    await user.click(within(bulkBar).getByRole('button', { name: 'Put in maintenance' }));

    const dialog = screen.getByRole('dialog', { name: 'Put chargers into maintenance' });
    await user.click(within(dialog).getByRole('button', { name: 'Put in maintenance' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/plugs/1', { status: 'maintenance' })
    );
  });
});

describe('QR code', () => {
  it('shows the driver deep link for the selected charger', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Bay 1');

    const rows = screen.getAllByRole('row');
    const row1 = rows.find((r) => within(r).queryByText('Bay 1'));
    await user.click(within(row1).getByRole('button', { name: 'QR' }));

    expect(screen.getByRole('dialog', { name: 'Charger QR code' })).toBeInTheDocument();
    expect(screen.getByText(/Opens AmpHive with this charger prefilled/)).toBeInTheDocument();
  });
});
