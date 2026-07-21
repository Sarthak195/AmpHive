/**
 * CpoPricing tests: loading/error states, the tariffs list (rate/slot-count/
 * assigned-count), create/edit/delete tariff, the per-tariff slot editor
 * (add slot incl. midnight-crossing auto-split, remove slot), the tenant
 * default select, and the group/charger assignment table.
 *
 * Note: tariff names ("Standard", "Peak Site") also appear as <option> text
 * in the assignment selects (tenant default + every group/charger row), so
 * row lookups are scoped to the tariffs table specifically via `tariffRow`.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoPricing from './CpoPricing';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', () => ({ useToast: () => toast }));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div>{children}</div>,
}));

vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => ({ coin_inr_rate: 1 }),
}));

const TARIFFS = [
  { id: 1, name: 'Standard', price_per_kwh: 5 },
  { id: 2, name: 'Peak Site', price_per_kwh: 8 },
];
const GROUPS = [{ id: 10, name: 'Sunrise Society', is_public: false, plug_count: 3, tariff_id: 1 }];
const PLUGS = [{ id: 100, name: 'Bay A1', group_id: 10, tariff_id: null }];
const PROFILE = { tenant: { name: 'Volt Yard', timezone: 'Asia/Kolkata', default_tariff_id: 1 } };
const SLOTS_BY_TARIFF = {
  1: [{ id: 500, tariff_id: 1, start_min: 1080, end_min: 1320, price_per_kwh: 8, days_mask: 127 }],
  2: [],
};

const routeGet = (url) => {
  if (url === '/api/cpo/tariffs') return Promise.resolve(TARIFFS);
  if (url === '/api/cpo/groups') return Promise.resolve(GROUPS);
  if (url === '/api/cpo/plugs') return Promise.resolve(PLUGS);
  if (url === '/api/cpo/profile') return Promise.resolve(PROFILE);
  const m = url.match(/\/api\/cpo\/tariffs\/(\d+)\/slots$/);
  if (m) return Promise.resolve(SLOTS_BY_TARIFF[Number(m[1])] || []);
  return Promise.reject(new Error(`unexpected GET ${url}`));
};

const renderPage = () => render(<CpoPricing />);

// Waits for the page to finish loading, then returns the tariffs <table> —
// the first of the (up to four) tables/table-role elements on the page.
const tariffsTable = async () => {
  await screen.findByRole('heading', { name: 'Tariffs' });
  return document.querySelectorAll('table')[0];
};

const tariffRow = async (name) => within(await tariffsTable()).getByText(name).closest('tr');

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockImplementation(routeGet);
  api.post.mockResolvedValue({ status: 'created' });
  api.put.mockResolvedValue({ status: 'updated' });
  api.delete.mockResolvedValue({ status: 'deleted' });
});

describe('loading and error', () => {
  it('shows a skeleton while the initial fetch is pending', () => {
    api.get.mockImplementation(() => new Promise(() => {}));
    renderPage();
    expect(document.querySelectorAll('.skeleton').length).toBeGreaterThan(0);
  });

  it('shows a retryable error instead of an empty page on failure', async () => {
    api.get.mockRejectedValue(new Error('down'));
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();

    api.get.mockImplementation(routeGet);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('heading', { name: 'Tariffs' })).toBeInTheDocument();
    expect(await tariffRow('Standard')).toBeTruthy();
  });
});

describe('tariffs list', () => {
  it('renders name, base rate, slot count and assigned-to count', async () => {
    renderPage();

    const standardRow = await tariffRow('Standard');
    const peakRow = await tariffRow('Peak Site');
    expect(within(standardRow).getByText('₹5.00')).toBeInTheDocument();
    expect(within(peakRow).getByText('₹8.00')).toBeInTheDocument();

    // Slot counts arrive from the background per-tariff fetch.
    await waitFor(() => expect(within(standardRow).getByText('1')).toBeInTheDocument());

    // Assigned-to: Standard = group(10) + tenant default = 2; Peak Site = 0.
    expect(within(standardRow).getByText('2')).toBeInTheDocument();
    expect(within(peakRow).getAllByText('0')).toHaveLength(2); // slots AND assigned-to
  });
});

describe('create tariff', () => {
  it('opens the modal, submits, toasts and refetches', async () => {
    renderPage();
    await tariffsTable();

    await userEvent.click(screen.getByRole('button', { name: /new tariff/i }));
    await userEvent.type(screen.getByLabelText('Name'), 'Weekend Rate');
    await userEvent.type(screen.getByLabelText('Base rate (coins/kWh)'), '6');
    await userEvent.click(screen.getByRole('button', { name: 'Create tariff' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/tariffs', {
        name: 'Weekend Rate',
        price_per_kwh: 6,
      })
    );
    expect(toast.ok).toHaveBeenCalledWith('Created "Weekend Rate".');
  });
});

describe('edit tariff', () => {
  it('prefills the form and PUTs the change', async () => {
    renderPage();
    const row = await tariffRow('Standard');

    await userEvent.click(within(row).getByRole('button', { name: 'Edit' }));
    const nameInput = screen.getByLabelText('Name');
    expect(nameInput).toHaveValue('Standard');

    await userEvent.clear(screen.getByLabelText('Base rate (coins/kWh)'));
    await userEvent.type(screen.getByLabelText('Base rate (coins/kWh)'), '7.5');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/tariffs/1', {
        name: 'Standard',
        price_per_kwh: 7.5,
      })
    );
  });
});

describe('delete tariff', () => {
  it('confirms with a fallback explanation and deletes', async () => {
    renderPage();
    const row = await tariffRow('Peak Site');

    await userEvent.click(within(row).getByRole('button', { name: 'Delete' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Delete "Peak Site"?');
    expect(screen.getByRole('dialog')).toHaveTextContent(/back to the next pricing rule/i);

    await userEvent.click(screen.getByRole('button', { name: 'Delete tariff' }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/cpo/tariffs/2'));
    expect(toast.ok).toHaveBeenCalledWith('Deleted "Peak Site".');
  });
});

describe('slot editor', () => {
  it('expands to show the slot list for the selected tariff', async () => {
    renderPage();
    const row = await tariffRow('Standard');

    await userEvent.click(within(row).getByRole('button', { name: 'Manage slots' }));
    expect(await screen.findByText('18:00–22:00')).toBeInTheDocument();

    const slotsTable = document.querySelectorAll('table')[1];
    expect(within(slotsTable).getByText('Every day')).toBeInTheDocument();
  });

  it('splits a midnight-crossing window into two slot creates', async () => {
    renderPage();
    const row = await tariffRow('Standard');
    await userEvent.click(within(row).getByRole('button', { name: 'Manage slots' }));
    await screen.findByText('18:00–22:00');

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '22:00' } });
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '06:00' } });
    await userEvent.type(screen.getByLabelText('Rate (coins/kWh)'), '9');
    await userEvent.click(screen.getByRole('button', { name: 'Add slot' }));

    await waitFor(() => expect(api.post).toHaveBeenCalledTimes(2));
    expect(api.post).toHaveBeenNthCalledWith(1, '/api/cpo/tariffs/1/slots', {
      start_min: 1320,
      end_min: 1440,
      price_per_kwh: 9,
      days_mask: 127,
    });
    expect(api.post).toHaveBeenNthCalledWith(2, '/api/cpo/tariffs/1/slots', {
      start_min: 0,
      end_min: 360,
      price_per_kwh: 9,
      days_mask: 127,
    });
  });

  it('removes a slot after confirmation', async () => {
    renderPage();
    const row = await tariffRow('Standard');
    await userEvent.click(within(row).getByRole('button', { name: 'Manage slots' }));
    await screen.findByText('18:00–22:00');

    await userEvent.click(screen.getByRole('button', { name: 'Remove' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Remove this slot?');
    await userEvent.click(screen.getByRole('button', { name: 'Remove slot' }));

    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/cpo/tariffs/1/slots/500'));
    expect(toast.ok).toHaveBeenCalledWith('Slot removed.');
  });
});

describe('assignment', () => {
  it('changes the tenant default tariff', async () => {
    renderPage();
    await tariffsTable();

    const select = screen.getByLabelText('Tenant default');
    expect(select).toHaveValue('1');
    await userEvent.selectOptions(select, '2');

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/tenant/default-tariff', { tariff_id: 2 })
    );
    expect(toast.ok).toHaveBeenCalledWith('Default tariff updated.');
  });

  it('reassigns a group and a charger via their row select', async () => {
    renderPage();
    await tariffsTable();

    const groupSelect = screen.getByLabelText('Pricing for Sunrise Society');
    expect(groupSelect).toHaveValue('1');
    await userEvent.selectOptions(groupSelect, '');
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/groups/10/tariff', { tariff_id: null })
    );

    const plugSelect = screen.getByLabelText('Pricing for Bay A1');
    expect(plugSelect).toHaveValue('');
    await userEvent.selectOptions(plugSelect, '2');
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/plugs/100/tariff', { tariff_id: 2 })
    );
  });
});
