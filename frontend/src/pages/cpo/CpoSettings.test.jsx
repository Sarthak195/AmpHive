/**
 * CpoSettings tests: the page loads the tenant's GST invoicing profile and
 * prefills the GSTIN / legal name / invoice-prefix fields, saves edited values
 * through PUT /api/cpo/profile with a success note, and surfaces a rejected
 * save inline.
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

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'cpo@amphive.test', full_name: 'Ops', role: 'cpo' } }),
}));

const PROFILE = {
  tenant: {
    name: 'Acme Charging',
    timezone: 'Asia/Kolkata',
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
  it('loads the profile and prefills the three fields', async () => {
    mockApi();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('GSTIN')).toHaveValue('22AAAAA0000A1Z5');
    });
    expect(screen.getByLabelText('Legal name (as on the GST certificate)')).toHaveValue('Acme Charging Pvt Ltd');
    expect(screen.getByLabelText('Invoice number prefix')).toHaveValue('ACME');
  });

  it('saves edited values and shows the success note', async () => {
    mockApi();
    api.put.mockResolvedValue({ status: 'saved' });
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('Invoice number prefix')).toHaveValue('ACME');
    });

    const prefix = screen.getByLabelText('Invoice number prefix');
    await user.clear(prefix);
    await user.type(prefix, 'HIVE');
    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(api.put).toHaveBeenCalledWith('/api/cpo/profile', {
      gstin: '22AAAAA0000A1Z5',
      legal_name: 'Acme Charging Pvt Ltd',
      invoice_prefix: 'HIVE',
    });
    expect(await screen.findByText('Saved.')).toBeInTheDocument();
  });

  it('surfaces a rejected save inline', async () => {
    mockApi();
    api.put.mockRejectedValue(new Error('GSTIN is not a valid format.'));
    const user = userEvent.setup();
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText('GSTIN')).toHaveValue('22AAAAA0000A1Z5');
    });

    await user.click(screen.getByRole('button', { name: 'Save' }));

    expect(await screen.findByText(/not a valid format/)).toBeInTheDocument();
  });
});
