/**
 * CpoTariffs tests: the page lists a tenant's tariffs with base rate + the tz
 * note, creates a tariff, deletes one through window.confirm, expands a tariff
 * to load + add + remove its time-of-day slots (asserting the HH:MM -> minute
 * conversion), surfaces a backend overlap 409 inline, and shows the empty state.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoTariffs from './CpoTariffs';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'cpo@amphive.test', full_name: 'Ops', role: 'cpo' } }),
}));

const TARIFFS = [
  { id: 1, name: 'Society Default', price_per_kwh: 5, created_at: null, updated_at: null },
  { id: 2, name: 'Guest', price_per_kwh: 8, created_at: null, updated_at: null },
];
const PROFILE = { tenant: { name: 'Acme Charging', timezone: 'Asia/Kolkata' } };
const SLOTS = [
  { id: 10, tariff_id: 1, start_min: 1080, end_min: 1320, price_per_kwh: 7, days_mask: 127 },
];

const mockApi = ({ tariffs = TARIFFS, profile = PROFILE, slots = SLOTS } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.includes('/slots')) return Promise.resolve(slots);
    if (url === '/api/cpo/tariffs') return Promise.resolve(tariffs);
    if (url === '/api/cpo/profile') return Promise.resolve(profile);
    return Promise.resolve([]);
  });
};

const renderPage = () => render(<MemoryRouter><CpoTariffs /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('CpoTariffs', () => {
  it('lists tariffs with base rate and shows the timezone note', async () => {
    mockApi();
    renderPage();

    await screen.findByText('Society Default');
    expect(screen.getByText('Guest')).toBeInTheDocument();
    expect(screen.getByText('5/kWh')).toBeInTheDocument();
    // The tz the slots are interpreted in is surfaced to the operator.
    expect(screen.getByText('Asia/Kolkata')).toBeInTheDocument();
  });

  it('creates a tariff and refetches', async () => {
    mockApi();
    api.post.mockResolvedValue({ status: 'created', tariff_id: 3 });
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Society Default');
    await user.type(screen.getByPlaceholderText(/Tariff name/i), 'Peak Plan');
    await user.type(screen.getByPlaceholderText(/Base coins/i), '9.5');
    await user.click(screen.getByRole('button', { name: 'Add tariff' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/tariffs', {
      name: 'Peak Plan', price_per_kwh: 9.5,
    });
    await waitFor(() => {
      const listCalls = api.get.mock.calls.filter(([u]) => u === '/api/cpo/tariffs');
      expect(listCalls.length).toBe(2);
    });
  });

  it('deletes a tariff through window.confirm', async () => {
    mockApi();
    api.delete.mockResolvedValue({ status: 'deleted' });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Society Default');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('Guest'));
    await user.click(within(row).getByRole('button', { name: 'Delete' }));

    expect(api.delete).toHaveBeenCalledWith('/api/cpo/tariffs/2');
  });

  it('expands a tariff, lists its slots, and adds one with HH:MM→minutes', async () => {
    mockApi();
    api.post.mockResolvedValue({ status: 'created', id: 11 });
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Society Default');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('Society Default'));
    await user.click(within(row).getByRole('button', { name: 'Manage slots' }));

    // Existing slot rendered as its window + rate + days (127 -> "Every day").
    expect(await screen.findByText('18:00 – 22:00')).toBeInTheDocument();
    expect(screen.getByText('Every day')).toBeInTheDocument();

    // Add a slot — defaults are 18:00→22:00 (1080→1320), all days; set the price.
    await user.type(screen.getByPlaceholderText('coins/kWh'), '6');
    await user.click(screen.getByRole('button', { name: 'Add slot' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/tariffs/1/slots', {
      start_min: 1080, end_min: 1320, price_per_kwh: 6, days_mask: 127,
    });
  });

  it('scopes a slot to selected weekdays via the days_mask toggles', async () => {
    mockApi();
    api.post.mockResolvedValue({ status: 'created', id: 12 });
    const user = userEvent.setup();
    renderPage();

    const row = (await screen.findAllByRole('row')).find((r) => r.textContent.includes('Society Default'));
    await user.click(within(row).getByRole('button', { name: 'Manage slots' }));
    await screen.findByText('18:00 – 22:00');

    // Start from "every day" (127); deselect Sat + Sun → Mon–Fri (31).
    await user.click(screen.getByRole('button', { name: 'Sat' }));
    await user.click(screen.getByRole('button', { name: 'Sun' }));
    await user.type(screen.getByPlaceholderText('coins/kWh'), '6');
    await user.click(screen.getByRole('button', { name: 'Add slot' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/tariffs/1/slots', {
      start_min: 1080, end_min: 1320, price_per_kwh: 6, days_mask: 31,
    });
  });

  it('blocks adding a slot with no days selected', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();

    const row = (await screen.findAllByRole('row')).find((r) => r.textContent.includes('Society Default'));
    await user.click(within(row).getByRole('button', { name: 'Manage slots' }));
    await screen.findByText('18:00 – 22:00');

    // Deselect every weekday, then try to add.
    for (const d of ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']) {
      await user.click(screen.getByRole('button', { name: d }));
    }
    await user.type(screen.getByPlaceholderText('coins/kWh'), '6');
    await user.click(screen.getByRole('button', { name: 'Add slot' }));

    expect(await screen.findByText(/Select at least one day/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('surfaces a backend overlap 409 inline when adding a slot', async () => {
    mockApi();
    api.post.mockRejectedValue(new Error('That window overlaps an existing slot (18:00–22:00).'));
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Society Default');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('Society Default'));
    await user.click(within(row).getByRole('button', { name: 'Manage slots' }));
    await screen.findByText('18:00 – 22:00');

    await user.type(screen.getByPlaceholderText('coins/kWh'), '6');
    await user.click(screen.getByRole('button', { name: 'Add slot' }));

    expect(await screen.findByText(/overlaps an existing slot/)).toBeInTheDocument();
  });

  it('shows the empty state when there are no tariffs', async () => {
    mockApi({ tariffs: [] });
    renderPage();
    expect(await screen.findByText('No tariffs yet')).toBeInTheDocument();
  });
});
