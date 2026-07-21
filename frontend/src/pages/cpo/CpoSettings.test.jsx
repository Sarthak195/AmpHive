/**
 * CpoSettings tests: three independently-saved cards. Organization identity
 * (name/timezone) renders read-only from the profile; the Defaults card
 * client-validates its two numeric fields before PUTting only its own
 * fields; the GST card PUTs only its own fields and shows a non-blocking
 * soft warning for a GSTIN that doesn't match the standard format; a load
 * failure surfaces a retryable ErrorState instead of an empty page.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoSettings from './CpoSettings';
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

const PROFILE = {
  tenant: {
    name: 'Acme Charging',
    timezone: 'Asia/Kolkata',
    queued_charging_enabled: false,
    auto_start_delay_min: 2,
    queue_ttl_min: 720,
    gstin: '22AAAAA0000A1Z5',
    legal_name: 'Acme Charging Pvt Ltd',
    invoice_prefix: 'ACME',
  },
};

const mockApi = ({ profile = PROFILE } = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/profile') return Promise.resolve(profile);
    return Promise.resolve([]);
  });
};

const renderPage = () => render(<MemoryRouter><CpoSettings /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('CpoSettings', () => {
  it('loads the profile and renders organization identity read-only', async () => {
    mockApi();
    renderPage();

    expect(await screen.findByText('Acme Charging')).toBeInTheDocument();
    expect(screen.getByText('Asia/Kolkata')).toBeInTheDocument();
    expect(screen.getByText('To rename your organization, contact support.')).toBeInTheDocument();
  });

  it('prefills the defaults and GST fields from the profile', async () => {
    mockApi();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('Auto-start debounce (minutes)')).toHaveValue(2);
    });
    expect(screen.getByLabelText('Queue lifetime (minutes)')).toHaveValue(720);
    expect(screen.getByRole('checkbox', { name: /queued charging/i })).not.toBeChecked();
    expect(screen.getByLabelText('GSTIN')).toHaveValue('22AAAAA0000A1Z5');
    expect(screen.getByLabelText('Legal name (as on the GST certificate)')).toHaveValue('Acme Charging Pvt Ltd');
    expect(screen.getByLabelText('Invoice number prefix')).toHaveValue('ACME');
  });

  it('shows a retryable error instead of an empty page on load failure', async () => {
    api.get.mockRejectedValue(new Error('down'));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load your settings")).toBeInTheDocument();

    mockApi();
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(await screen.findByText('Acme Charging')).toBeInTheDocument();
  });

  it('saves the charging defaults with only their own fields and toasts success', async () => {
    mockApi();
    api.put.mockResolvedValue({
      status: 'updated',
      queued_charging_enabled: true,
      auto_start_delay_min: 5,
      queue_ttl_min: 60,
      gstin: '22AAAAA0000A1Z5',
      legal_name: 'Acme Charging Pvt Ltd',
      invoice_prefix: 'ACME',
    });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('Queue lifetime (minutes)')).toHaveValue(720);
    });

    await user.click(screen.getByRole('checkbox', { name: /queued charging/i }));
    const delay = screen.getByLabelText('Auto-start debounce (minutes)');
    await user.clear(delay);
    await user.type(delay, '5');
    const ttl = screen.getByLabelText('Queue lifetime (minutes)');
    await user.clear(ttl);
    await user.type(ttl, '60');

    await user.click(screen.getAllByRole('button', { name: 'Save changes' })[0]);

    expect(api.put).toHaveBeenCalledWith('/api/cpo/profile', {
      queued_charging_enabled: true,
      auto_start_delay_min: 5,
      queue_ttl_min: 60,
    });
    expect(toast.ok).toHaveBeenCalledWith('Charging defaults saved.');
  });

  it('rejects an out-of-range queue lifetime client-side without calling the API', async () => {
    mockApi();
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('Queue lifetime (minutes)')).toHaveValue(720);
    });

    const ttl = screen.getByLabelText('Queue lifetime (minutes)');
    await user.clear(ttl);
    await user.type(ttl, '99999');

    const forms = screen.getAllByRole('button', { name: 'Save changes' });
    await user.click(forms[0]);

    expect(await screen.findByText(/between 1 and 43200 minutes/)).toBeInTheDocument();
    expect(api.put).not.toHaveBeenCalled();
  });

  it('saves the GST card with only its own fields and toasts success', async () => {
    mockApi();
    api.put.mockResolvedValue({ status: 'updated', gstin: 'HIVE', legal_name: 'Acme Charging Pvt Ltd', invoice_prefix: 'HIVE' });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('Invoice number prefix')).toHaveValue('ACME');
    });

    const prefix = screen.getByLabelText('Invoice number prefix');
    await user.clear(prefix);
    await user.type(prefix, 'HIVE');

    const buttons = screen.getAllByRole('button', { name: 'Save changes' });
    await user.click(buttons[1]);

    expect(api.put).toHaveBeenCalledWith('/api/cpo/profile', {
      gstin: '22AAAAA0000A1Z5',
      legal_name: 'Acme Charging Pvt Ltd',
      invoice_prefix: 'HIVE',
    });
    expect(toast.ok).toHaveBeenCalledWith('Invoicing details saved.');
  });

  it('shows a non-blocking soft warning for a GSTIN that does not match the standard format', async () => {
    mockApi({ profile: { tenant: { ...PROFILE.tenant, gstin: '' } } });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('GSTIN')).toHaveValue('');
    });

    await user.type(screen.getByLabelText('GSTIN'), 'NOTAGSTIN');

    expect(await screen.findByText(/Doesn't look like a standard GSTIN/)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Save changes' })[1]).not.toBeDisabled();
  });

  it('surfaces a rejected save as an error toast', async () => {
    mockApi();
    api.put.mockRejectedValue(new Error('GSTIN is not a valid format.'));
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('GSTIN')).toHaveValue('22AAAAA0000A1Z5');
    });

    const buttons = screen.getAllByRole('button', { name: 'Save changes' });
    await user.click(buttons[1]);

    expect(toast.error).toHaveBeenCalledWith('GSTIN is not a valid format.');
  });
});
