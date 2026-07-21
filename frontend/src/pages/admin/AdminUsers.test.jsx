/**
 * AdminUsers tests: the user list renders from GET /api/admin/users with
 * search/role filters, surfaces a retryable ErrorState (never a blank
 * table) on failure, and the three row mutations — change role,
 * disable/enable, adjust balance — call the right endpoints with the right
 * bodies, update the row in place, and toast on success. The signed-in
 * admin's own row can't touch role/disable (mirrors the backend's
 * self-protection 403).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import AdminUsers from './AdminUsers';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', () => ({
  useToast: () => toast,
}));

let mockUser = { id: 1, email: 'admin@amphive.test' };
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: mockUser }),
}));

let mockConfig = { coin_inr_rate: 1 };
vi.mock('../../contexts/ConfigContext', () => ({
  useConfig: () => mockConfig,
}));

const USERS = {
  total: 2,
  items: [
    {
      id: 1,
      email: 'admin@amphive.test',
      full_name: 'Admin Account',
      role: 'admin',
      tenant_id: null,
      tenant_name: null,
      coin_balance: 0,
      is_disabled: false,
      created_at: '2026-01-01T00:00:00Z',
    },
    {
      id: 7,
      email: 'driver@amphive.test',
      full_name: 'Test Driver',
      role: 'driver',
      tenant_id: 3,
      tenant_name: 'Acme Society',
      coin_balance: 500,
      is_disabled: false,
      created_at: '2026-02-01T00:00:00Z',
    },
  ],
};

const renderPage = () => render(<AdminUsers />);

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { id: 1, email: 'admin@amphive.test' };
  mockConfig = { coin_inr_rate: 1 };
  api.get.mockResolvedValue(USERS);
});

describe('list', () => {
  it('renders users with role/status badges, org, and balance', async () => {
    renderPage();

    expect(await screen.findByText('driver@amphive.test')).toBeInTheDocument();
    expect(screen.getByText('Acme Society')).toBeInTheDocument();
    expect(screen.getByText('₹500.00')).toBeInTheDocument();
    expect(screen.getAllByText('Active')).toHaveLength(2);
    const table = screen.getByRole('table');
    expect(within(table).getByText('Driver')).toBeInTheDocument();
    expect(within(table).getByText('Admin')).toBeInTheDocument();
    expect(screen.getByText('2 users')).toBeInTheDocument();

    expect(api.get).toHaveBeenCalledWith('/api/admin/users?limit=20&offset=0');
  });

  it('surfaces a retryable ErrorState instead of a blank table on failure', async () => {
    api.get.mockRejectedValueOnce(new Error('boom'));
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();

    api.get.mockResolvedValueOnce(USERS);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('driver@amphive.test')).toBeInTheDocument();
  });

  it('applies the role filter immediately', async () => {
    renderPage();
    await screen.findByText('driver@amphive.test');
    api.get.mockClear();

    await userEvent.selectOptions(screen.getByLabelText('Filter by role'), 'driver');

    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith('/api/admin/users?limit=20&offset=0&role=driver')
    );
  });

  it('debounces the search box before querying', async () => {
    renderPage();
    await screen.findByText('driver@amphive.test');
    api.get.mockClear();

    await userEvent.type(screen.getByLabelText('Search users'), 'acme');

    // Not called immediately.
    expect(api.get).not.toHaveBeenCalled();
    await waitFor(
      () => expect(api.get).toHaveBeenCalledWith('/api/admin/users?limit=20&offset=0&q=acme'),
      { timeout: 2000 }
    );
  });
});

describe('change role', () => {
  it("disables the action on the signed-in admin's own row", async () => {
    renderPage();
    await screen.findByText('driver@amphive.test');

    const rows = screen.getAllByRole('row');
    const ownRow = rows.find((r) => within(r).queryByText('admin@amphive.test'));
    expect(within(ownRow).getByRole('button', { name: /change role/i })).toBeDisabled();
    expect(within(ownRow).getByRole('button', { name: /disable/i })).toBeDisabled();
  });

  it('opens a picker excluding the current role and patches on confirm', async () => {
    renderPage();
    await screen.findByText('driver@amphive.test');

    const rows = screen.getAllByRole('row');
    const driverRow = rows.find((r) => within(r).queryByText('driver@amphive.test'));
    await userEvent.click(within(driverRow).getByRole('button', { name: /change role/i }));

    const dialog = screen.getByRole('dialog', { name: 'Change role' });
    const select = screen.getByLabelText('New role');
    // Current role (driver) must not be offered as a target.
    expect(within(select).queryByRole('option', { name: 'Driver' })).not.toBeInTheDocument();

    await userEvent.selectOptions(select, 'cpo');
    expect(
      screen.getByText(/Driver → CPO requires an organization/i)
    ).toBeInTheDocument();

    api.patch.mockResolvedValue({ status: 'updated', id: 7, role: 'cpo', is_disabled: false });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Change role' }));

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/api/admin/users/7', { role: 'cpo' })
    );
    expect(toast.ok).toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

describe('disable / enable', () => {
  it('confirms with the exact consequence copy and patches is_disabled', async () => {
    renderPage();
    await screen.findByText('driver@amphive.test');

    const rows = screen.getAllByRole('row');
    const driverRow = rows.find((r) => within(r).queryByText('driver@amphive.test'));
    await userEvent.click(within(driverRow).getByRole('button', { name: /disable/i }));

    expect(
      screen.getByText(
        "Disable driver@amphive.test? They're signed out everywhere immediately. A charge already in progress keeps running until it finishes or is stopped separately."
      )
    ).toBeInTheDocument();

    api.patch.mockResolvedValue({ status: 'updated', id: 7, role: 'driver', is_disabled: true });
    await userEvent.click(screen.getByRole('button', { name: 'Disable account' }));

    await waitFor(() =>
      expect(api.patch).toHaveBeenCalledWith('/api/admin/users/7', { is_disabled: true })
    );
    expect(toast.ok).toHaveBeenCalled();
  });
});

describe('adjust balance', () => {
  it('rejects an empty amount without calling the API', async () => {
    renderPage();
    await screen.findByText('driver@amphive.test');

    const rows = screen.getAllByRole('row');
    const driverRow = rows.find((r) => within(r).queryByText('driver@amphive.test'));
    await userEvent.click(within(driverRow).getByRole('button', { name: /adjust balance/i }));

    const dialog = screen.getByRole('dialog', { name: /Adjust balance/ });
    await userEvent.type(screen.getByLabelText('Reason'), 'goodwill credit');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Adjust balance' }));

    expect(await screen.findByText(/Enter a non-zero/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('converts ₹ to coins at the configured rate, posts the reason, and shows the resulting balance', async () => {
    mockConfig = { coin_inr_rate: 2 };
    renderPage();
    await screen.findByText('driver@amphive.test');

    const rows = screen.getAllByRole('row');
    const driverRow = rows.find((r) => within(r).queryByText('driver@amphive.test'));
    await userEvent.click(within(driverRow).getByRole('button', { name: /adjust balance/i }));

    const dialog = screen.getByRole('dialog', { name: /Adjust balance/ });
    await userEvent.type(screen.getByLabelText('Amount (₹)'), '100');
    await userEvent.type(screen.getByLabelText('Reason'), 'goodwill credit');

    api.post.mockResolvedValue({ new_balance: 550 });
    await userEvent.click(within(dialog).getByRole('button', { name: 'Adjust balance' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/admin/users/7/adjust-balance', {
        amount_coins: 50,
        reason: 'goodwill credit',
      })
    );
    expect(toast.ok).toHaveBeenCalledWith(expect.stringContaining('₹1,100.00'));
  });
});
