/**
 * CpoPricing tests: loading/error states, the tariffs list (rate/slot-count/
 * assigned-count), create/edit/delete tariff, the per-tariff slot editor
 * (add slot incl. midnight-crossing auto-split, remove slot), the tenant
 * default select, the group/charger assignment table, and the Simple/
 * Advanced mode split (smart detection + the Simple-mode "broadcast" save —
 * see also CpoPricingSimple.test.jsx for the Simple view in isolation).
 *
 * Note: tariff names ("Standard", "Peak Site") also appear as <option> text
 * in the assignment selects (tenant default + every group/charger row), so
 * row lookups are scoped to the tariffs table specifically via `tariffRow`.
 *
 * The shared TARIFFS fixture below has two tariffs, so it's always "custom"
 * per the smart-detection rule (utils/pricingUniformity) — every test that
 * reuses it lands in Advanced automatically, exactly like before this split
 * existed. The dedicated "Simple/Advanced smart detection" describe block
 * below swaps in single-/zero-tariff fixtures to exercise the Simple-default
 * path.
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
  useConfig: () => ({ coin_inr_rate: 1, coins_per_kwh: 5 }),
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

/* ---- Simple/Advanced mode ------------------------------------------------- */

// Builds a routeGet function for an arbitrary tariffs/groups/plugs/profile/
// slots fixture, same shape as the shared `routeGet` above.
const routeGetWith = (tariffs, groups, plugs, profile, slotsByTariff) => (url) => {
  if (url === '/api/cpo/tariffs') return Promise.resolve(tariffs);
  if (url === '/api/cpo/groups') return Promise.resolve(groups);
  if (url === '/api/cpo/plugs') return Promise.resolve(plugs);
  if (url === '/api/cpo/profile') return Promise.resolve(profile);
  const m = url.match(/\/api\/cpo\/tariffs\/(\d+)\/slots$/);
  if (m) return Promise.resolve((slotsByTariff && slotsByTariff[Number(m[1])]) || []);
  return Promise.reject(new Error(`unexpected GET ${url}`));
};

describe('Simple/Advanced smart detection', () => {
  it('opens in Advanced by default for a non-uniform tenant (two tariffs)', async () => {
    renderPage();
    await tariffsTable();

    expect(screen.getByRole('tab', { name: 'Advanced' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('tab', { name: 'Simple' })).toHaveAttribute('aria-selected', 'false');
  });

  it('shows a "custom schedule active" summary when switched to Simple manually', async () => {
    renderPage();
    await tariffsTable();

    await userEvent.click(screen.getByRole('tab', { name: 'Simple' }));
    expect(await screen.findByText('Custom schedule active.')).toBeInTheDocument();
    // Prefilled from the tenant default tariff ("Standard", ₹5) as a sane
    // starting point for "replace the custom schedule with…".
    expect(screen.getByLabelText('Price per kWh (₹)')).toHaveValue(5);
  });

  it('opens in Simple, prefilled at the platform default rate, when the tenant has no tariff at all', async () => {
    api.get.mockImplementation(
      routeGetWith(
        [],
        [{ id: 20, name: 'Only Group', tariff_id: null }],
        [],
        { tenant: { name: 'Solo CPO', timezone: '', default_tariff_id: null } },
        {}
      )
    );
    renderPage();

    const defaultRateInput = await screen.findByLabelText('Price per kWh (₹)');
    // The prefill lands a render after the slot-count fetch settles — wait for
    // the value rather than racing it (was a CI-flaky assertion).
    await waitFor(() => expect(defaultRateInput).toHaveValue(5));
    expect(screen.getByRole('tab', { name: 'Simple' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByText('Custom schedule active.')).not.toBeInTheDocument();
  });

  it('opens in Simple, prefilled at its rate, for a single default tariff with no slots/overrides', async () => {
    api.get.mockImplementation(
      routeGetWith(
        [{ id: 5, name: 'Standard', price_per_kwh: 6 }],
        [{ id: 30, name: 'Only Group', tariff_id: null }],
        [{ id: 300, name: 'Only Plug', tariff_id: null }],
        { tenant: { name: 'Solo CPO', timezone: 'Asia/Kolkata', default_tariff_id: 5 } },
        { 5: [] }
      )
    );
    renderPage();

    const tariffRateInput = await screen.findByLabelText('Price per kWh (₹)');
    // Same slot-fetch race as above — wait for the prefill instead of racing it.
    await waitFor(() => expect(tariffRateInput).toHaveValue(6));
    expect(screen.getByRole('tab', { name: 'Simple' })).toHaveAttribute('aria-selected', 'true');
  });
});

describe('Simple mode save (broadcast)', () => {
  it('flattens the default tariff: updates its price and strips its time-of-day slots', async () => {
    renderPage(); // shared 2-tariff fixture: Standard(1, ₹5, 1 slot) is the tenant default
    await tariffsTable();
    await userEvent.click(screen.getByRole('tab', { name: 'Simple' }));
    await screen.findByText('Custom schedule active.');

    await userEvent.clear(screen.getByLabelText('Price per kWh (₹)'));
    await userEvent.type(screen.getByLabelText('Price per kWh (₹)'), '9');
    await userEvent.click(screen.getByRole('button', { name: 'Save price' }));
    await userEvent.click(screen.getByRole('button', { name: 'Replace with flat rate' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/tariffs/1', {
        name: 'Standard',
        price_per_kwh: 9,
      })
    );
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/cpo/tariffs/1/slots/500'));
    expect(api.put).toHaveBeenCalledWith('/api/cpo/tenant/default-tariff', { tariff_id: 1 });
    // Group 10 already resolves to tariff 1 and plug 100 already inherits —
    // neither is an "override" pinned to a different tariff, so no calls.
    expect(api.put).not.toHaveBeenCalledWith('/api/cpo/groups/10/tariff', expect.anything());
    expect(api.put).not.toHaveBeenCalledWith('/api/cpo/plugs/100/tariff', expect.anything());
    expect(toast.ok).toHaveBeenCalledWith('Price updated — applied to every charger.');
  });

  it('clears group/charger overrides pinned to a different tariff so the flat rate applies everywhere', async () => {
    api.get.mockImplementation(
      routeGetWith(
        [
          { id: 1, name: 'Standard', price_per_kwh: 5 },
          { id: 2, name: 'Peak Site', price_per_kwh: 8 },
        ],
        [{ id: 10, name: 'Sunrise Society', tariff_id: 2 }],
        [{ id: 100, name: 'Bay A1', tariff_id: 2 }],
        { tenant: { name: 'Volt Yard', timezone: 'Asia/Kolkata', default_tariff_id: 1 } },
        { 1: [], 2: [] }
      )
    );
    renderPage();
    await tariffsTable();
    await userEvent.click(screen.getByRole('tab', { name: 'Simple' }));
    await screen.findByText('Custom schedule active.');

    await userEvent.click(screen.getByRole('button', { name: 'Save price' }));
    await userEvent.click(screen.getByRole('button', { name: 'Replace with flat rate' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/groups/10/tariff', { tariff_id: null })
    );
    expect(api.put).toHaveBeenCalledWith('/api/cpo/plugs/100/tariff', { tariff_id: null });
    expect(api.put).toHaveBeenCalledWith('/api/cpo/tenant/default-tariff', { tariff_id: 1 });
  });

  it('creates a tariff when saving Simple for a tenant with none yet', async () => {
    api.get.mockImplementation(
      routeGetWith([], [], [], { tenant: { name: 'Solo CPO', timezone: '', default_tariff_id: null } }, {})
    );
    api.post.mockResolvedValue({ status: 'created', tariff_id: 42, name: 'Standard rate', price_per_kwh: 7 });
    renderPage();
    const priceInput = await screen.findByLabelText('Price per kWh (₹)');
    // Wait for the async seed price (5) to settle before typing. Otherwise
    // `clear` runs against the still-empty input (a no-op that never marks the
    // field dirty), the seed then lands as "5" mid-interaction, and `type('7')`
    // appends to it → "57" — the recurring CpoPricing CI flake.
    await waitFor(() => expect(priceInput).toHaveValue(5));

    await userEvent.clear(priceInput);
    await userEvent.type(priceInput, '7');
    await userEvent.click(screen.getByRole('button', { name: 'Save price' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/tariffs', {
        name: 'Standard rate',
        price_per_kwh: 7,
      })
    );
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/tenant/default-tariff', { tariff_id: 42 })
    );
  });
});
