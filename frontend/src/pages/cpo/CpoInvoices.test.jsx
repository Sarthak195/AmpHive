/**
 * CpoInvoices tests: the page lists a tenant's issued GST invoices with their
 * number + totals, shows the empty state when none exist, and opens the printable
 * HTML copy for a session via a raw (token-bearing) fetch when "View" is clicked.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoInvoices from './CpoInvoices';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'cpo@amphive.test', full_name: 'Ops', role: 'cpo' } }),
}));

const INVOICES = [
  {
    id: 1,
    invoice_number: 'INV-2026-0001',
    session_id: 42,
    issued_at: '2026-07-15T10:00:00Z',
    amount_coins: 100,
    taxable_value_inr: 84.75,
    gst_rate_pct: 18,
    gst_amount_inr: 15.25,
    total_inr: 100,
    currency: 'INR',
    seller: { legal_name: 'Acme Charging Pvt Ltd', gstin: '29ABCDE1234F1Z5' },
    line_item: { energy_kwh: 12.5, rate_coins_per_kwh: 8, description: 'EV charging' },
  },
  {
    id: 2,
    invoice_number: 'INV-2026-0002',
    session_id: 43,
    issued_at: '2026-07-16T12:30:00Z',
    amount_coins: 50,
    taxable_value_inr: 42.37,
    gst_rate_pct: 18,
    gst_amount_inr: 7.63,
    total_inr: 50,
    currency: 'INR',
    seller: { legal_name: 'Acme Charging Pvt Ltd', gstin: '29ABCDE1234F1Z5' },
    line_item: { energy_kwh: 6.25, rate_coins_per_kwh: 8, description: 'EV charging' },
  },
];
const PROFILE = { tenant: { name: 'Acme Charging', timezone: 'Asia/Kolkata' } };

const mockApi = ({ invoices = INVOICES, profile = PROFILE } = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/invoices') return Promise.resolve(invoices);
    if (url === '/api/cpo/profile') return Promise.resolve(profile);
    return Promise.resolve([]);
  });
};

const renderPage = () => render(<MemoryRouter><CpoInvoices /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('CpoInvoices', () => {
  it('lists invoices with their number and total', async () => {
    mockApi();
    renderPage();

    await screen.findByText('INV-2026-0001');
    expect(screen.getByText('INV-2026-0002')).toBeInTheDocument();
    expect(screen.getByText('100')).toBeInTheDocument();
    expect(screen.getByText('50')).toBeInTheDocument();
  });

  it('shows the empty state when there are no invoices', async () => {
    mockApi({ invoices: [] });
    renderPage();
    expect(await screen.findByText('No invoices yet')).toBeInTheDocument();
  });

  it('opens the printable HTML invoice via a raw token-bearing fetch on View', async () => {
    mockApi();
    const fetchMock = vi.fn().mockResolvedValue({ blob: () => Promise.resolve(new Blob()) });
    vi.stubGlobal('fetch', fetchMock);
    const createObjectURL = vi.fn().mockReturnValue('blob:mock');
    vi.stubGlobal('URL', { ...URL, createObjectURL });
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {});
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('INV-2026-0001');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('INV-2026-0001'));
    await user.click(within(row).getByRole('button', { name: 'View' }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/sessions/42/invoice?format=html'),
        expect.objectContaining({ headers: expect.objectContaining({ Authorization: expect.any(String) }) }),
      );
    });
    expect(openSpy).toHaveBeenCalled();
  });
});
