/**
 * CpoInvoices tests (redesign v3, D7): the paginated invoice table (number,
 * date, session, energy, ₹ total/GST), skeleton -> ErrorState-with-retry,
 * the empty state, exporting CSV via a raw authenticated fetch with the
 * chosen period, and opening the printable HTML invoice via a raw
 * token-bearing fetch on "View".
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoInvoices from './CpoInvoices';
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

const INVOICES = {
  total: 2,
  items: [
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
  ],
};

const mockApi = ({ invoices = INVOICES } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/invoices')) return Promise.resolve(invoices);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = () => render(<CpoInvoices />);

beforeEach(() => {
  vi.clearAllMocks();
  mockApi();
});
afterEach(() => vi.restoreAllMocks());

describe('CpoInvoices', () => {
  it('lists invoices with number, date, session, energy and totals', async () => {
    renderPage();

    await screen.findByText('INV-2026-0001');
    expect(screen.getByText('INV-2026-0002')).toBeInTheDocument();
    expect(screen.getByText('#42')).toBeInTheDocument();
    expect(screen.getByText('12.50 kWh')).toBeInTheDocument();
    expect(screen.getByText('₹100.00')).toBeInTheDocument();
    expect(screen.getByText('₹15.25')).toBeInTheDocument();
  });

  it('fetches with the paginated limit/offset params', async () => {
    renderPage();
    await screen.findByText('INV-2026-0001');
    expect(api.get).toHaveBeenCalledWith('/api/cpo/invoices?limit=20&offset=0');
  });

  it('shows the empty state when there are no invoices', async () => {
    mockApi({ invoices: { total: 0, items: [] } });
    renderPage();
    expect(await screen.findByText('No invoices yet')).toBeInTheDocument();
  });

  it('shows a retryable ErrorState on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/invoices')) return Promise.reject(new Error('Network down'));
      return Promise.reject(new Error('unhandled'));
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    mockApi();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('INV-2026-0001');
  });

  it('exports CSV via a raw authenticated fetch with the selected period', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(new Blob()) });
    vi.stubGlobal('fetch', fetchMock);
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn().mockReturnValue('blob:mock'), revokeObjectURL: vi.fn() });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('INV-2026-0001');

    await user.selectOptions(screen.getByLabelText('Export period'), '90');
    await user.click(screen.getByRole('button', { name: 'Export CSV' }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/cpo/invoices.csv?days=90'),
        expect.objectContaining({ headers: expect.any(Object) })
      );
    });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('opens the printable HTML invoice via a raw token-bearing fetch on View, then revokes the blob URL', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, blob: () => Promise.resolve(new Blob()) });
    vi.stubGlobal('fetch', fetchMock);
    const revokeSpy = vi.fn();
    vi.stubGlobal('URL', { ...URL, createObjectURL: vi.fn().mockReturnValue('blob:mock'), revokeObjectURL: revokeSpy });
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => {});
    const setTimeoutSpy = vi.spyOn(window, 'setTimeout');
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('INV-2026-0001');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('INV-2026-0001'));
    await user.click(within(row).getByRole('button', { name: 'View' }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/sessions/42/invoice?format=html'),
        expect.objectContaining({ headers: expect.any(Object) })
      );
    });
    expect(openSpy).toHaveBeenCalledWith('blob:mock', '_blank');

    // Revocation is deferred (not immediate, unlike the CSV anchor-download
    // path) so the new tab has time to load the blob first.
    expect(revokeSpy).not.toHaveBeenCalled();
    const deferredRevoke = setTimeoutSpy.mock.calls.find(([, delay]) => delay === 60_000);
    expect(deferredRevoke).toBeTruthy();
    deferredRevoke[0]();
    expect(revokeSpy).toHaveBeenCalledWith('blob:mock');
  });

  it('surfaces a failed invoice view as a toast', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: false, status: 404 });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('INV-2026-0001');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('INV-2026-0001'));
    await user.click(within(row).getByRole('button', { name: 'View' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
  });
});
