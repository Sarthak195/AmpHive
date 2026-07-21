/**
 * CpoSetup tests: an already-CPO user (or an admin who already has a
 * tenant_id) is redirected straight to the dashboard; a plain admin with no
 * tenant has nothing to set up here and is sent to /admin instead; a driver
 * sees the org-name form + what-happens-next checklist, blank submission is
 * blocked client-side, a successful POST /api/cpo/setup refreshes the user,
 * toasts, and redirects to the dashboard, and a rejected setup surfaces
 * inline (not as a toast).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import CpoSetup from './CpoSetup';
import { useAuth } from '../../contexts/AuthContext';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', () => ({ useToast: () => toast }));

const refreshUserSpy = vi.fn().mockResolvedValue(undefined);

const renderSetup = () =>
  render(
    <MemoryRouter initialEntries={['/cpo']}>
      <Routes>
        <Route path="/cpo" element={<CpoSetup />} />
        <Route path="/cpo/dashboard" element={<div>cpo dashboard</div>} />
        <Route path="/admin" element={<div>admin overview</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ user: { role: 'driver' }, refreshUser: refreshUserSpy });
});

describe('CpoSetup', () => {
  it('redirects a user who is already a CPO straight to the dashboard', () => {
    useAuth.mockReturnValue({ user: { role: 'cpo' }, refreshUser: refreshUserSpy });
    renderSetup();
    expect(screen.getByText('cpo dashboard')).toBeInTheDocument();
  });

  it('redirects an admin who already has a tenant_id straight to the dashboard', () => {
    useAuth.mockReturnValue({ user: { role: 'admin', tenant_id: 3 }, refreshUser: refreshUserSpy });
    renderSetup();
    expect(screen.getByText('cpo dashboard')).toBeInTheDocument();
  });

  it('sends a tenant-less admin to /admin instead of showing the become-a-host form', () => {
    useAuth.mockReturnValue({ user: { role: 'admin', tenant_id: null }, refreshUser: refreshUserSpy });
    renderSetup();
    expect(screen.getByText('admin overview')).toBeInTheDocument();
  });

  it('renders the organization form and the what-happens-next checklist', () => {
    renderSetup();
    expect(screen.getByRole('heading', { name: 'Set up your organization' })).toBeInTheDocument();
    expect(screen.getByLabelText('Organization name')).toBeInTheDocument();
    expect(screen.getByText(/1\. Add your chargers/)).toBeInTheDocument();
    expect(screen.getByText(/2\. Set your pricing/)).toBeInTheDocument();
    expect(screen.getByText(/3\. Create groups/)).toBeInTheDocument();
  });

  it('blocks submit client-side on a blank name without calling the API', async () => {
    const user = userEvent.setup();
    renderSetup();

    await user.click(screen.getByRole('button', { name: 'Create organization' }));

    expect(api.post).not.toHaveBeenCalled();
    expect(screen.getByText('Enter your organization name.')).toBeInTheDocument();
  });

  it('creates the organization, refreshes the user, toasts, and redirects', async () => {
    api.post.mockResolvedValue({ status: 'success', tenant_name: 'Sunrise Apartments' });
    const user = userEvent.setup();
    renderSetup();

    await user.type(screen.getByLabelText('Organization name'), 'Sunrise Apartments');
    await user.click(screen.getByRole('button', { name: 'Create organization' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/setup', { tenant_name: 'Sunrise Apartments' });
    expect(await screen.findByText('cpo dashboard')).toBeInTheDocument();
    expect(refreshUserSpy).toHaveBeenCalled();
    expect(toast.ok).toHaveBeenCalledWith('Organization "Sunrise Apartments" created.');
  });

  it('surfaces a rejected setup inline rather than as a toast', async () => {
    api.post.mockRejectedValue(new Error('That organization name is already taken.'));
    const user = userEvent.setup();
    renderSetup();

    await user.type(screen.getByLabelText('Organization name'), 'Sunrise Apartments');
    await user.click(screen.getByRole('button', { name: 'Create organization' }));

    expect(await screen.findByText('That organization name is already taken.')).toBeInTheDocument();
    expect(toast.error).not.toHaveBeenCalled();
  });
});
