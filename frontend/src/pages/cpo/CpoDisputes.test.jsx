/**
 * CpoDisputes tests: the page lists a tenant's session disputes with their
 * reason + a status badge, approves an OPEN dispute through window.confirm
 * (posting { action: 'approve' } with no refund_coins so the backend refunds
 * the full session cost) and refetches, surfaces a resolve 409 inline, and
 * shows the empty state when there are no disputes.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoDisputes from './CpoDisputes';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'cpo@amphive.test', full_name: 'Ops', role: 'cpo' } }),
}));

const DISPUTES = [
  {
    id: 1, session_id: 101, tenant_id: 1, driver_user_id: 42,
    reason: 'Charger stopped early', status: 'open',
    resolution_note: null, refund_coins: null,
    created_at: '2026-07-10T10:00:00Z', resolved_at: null, resolved_by_user_id: null,
  },
  {
    id: 2, session_id: 102, tenant_id: 1, driver_user_id: 43,
    reason: 'Double charged', status: 'approved',
    resolution_note: 'Verified overbill', refund_coins: 12,
    created_at: '2026-07-09T08:30:00Z', resolved_at: '2026-07-09T09:00:00Z', resolved_by_user_id: 5,
  },
];
const PROFILE = { tenant: { name: 'Acme Charging', timezone: 'Asia/Kolkata' } };

const mockApi = ({ disputes = DISPUTES, profile = PROFILE } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/disputes')) return Promise.resolve(disputes);
    if (url === '/api/cpo/profile') return Promise.resolve(profile);
    return Promise.resolve([]);
  });
};

const renderPage = () => render(<MemoryRouter><CpoDisputes /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('CpoDisputes', () => {
  it('lists disputes with their reason and a status badge', async () => {
    mockApi();
    renderPage();

    await screen.findByText('Charger stopped early');
    expect(screen.getByText('Double charged')).toBeInTheDocument();
    // Status is surfaced as a badge, and the approved row shows its refund.
    expect(screen.getByText('open')).toBeInTheDocument();
    expect(screen.getByText('approved')).toBeInTheDocument();
    expect(screen.getByText(/refunded 12/)).toBeInTheDocument();
  });

  it('approves an open dispute through window.confirm and refetches', async () => {
    mockApi();
    api.post.mockResolvedValue({ status: 'approved', refund_coins: 20 });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Charger stopped early');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('Charger stopped early'));
    await user.click(within(row).getByRole('button', { name: 'Approve' }));

    // No refund_coins in the body -> backend refunds the full session cost.
    expect(api.post).toHaveBeenCalledWith('/api/cpo/disputes/1/resolve', { action: 'approve' });
    await waitFor(() => {
      const listCalls = api.get.mock.calls.filter(([u]) => u.startsWith('/api/cpo/disputes'));
      expect(listCalls.length).toBe(2);
    });
  });

  it('surfaces a resolve 409 inline when the dispute is already resolved', async () => {
    mockApi();
    api.post.mockRejectedValue(new Error('This dispute has already been resolved.'));
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Charger stopped early');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('Charger stopped early'));
    await user.click(within(row).getByRole('button', { name: 'Approve' }));

    expect(await screen.findByText(/already been resolved/)).toBeInTheDocument();
  });

  it('shows the empty state when there are no disputes', async () => {
    mockApi({ disputes: [] });
    renderPage();
    expect(await screen.findByText('No disputes')).toBeInTheDocument();
  });
});
