/**
 * AdminAudit tests: renders a cross-tenant audit log table, filters by
 * organization, pages via DataTable pagination, and shows a retryable error
 * when the list fails to load.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import AdminAudit from './AdminAudit';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const TENANT_A = {
  id: 1,
  name: 'Acme Charging',
  created_at: '2026-01-10T00:00:00Z',
  user_count: 12,
  gateway_count: 5,
  gateways_online: 4,
  plug_count: 9,
  sessions_30d: 210,
  revenue_30d_coins: 15400,
  pending_payouts: 2,
};

const TENANT_B = {
  id: 2,
  name: 'Volt Retail',
  created_at: '2026-02-01T00:00:00Z',
  user_count: 3,
  gateway_count: 1,
  gateways_online: 0,
  plug_count: 2,
  sessions_30d: 0,
  revenue_30d_coins: 0,
  pending_payouts: 0,
};

const AUDIT_A = {
  id: 1,
  tenant_name: 'Acme Charging',
  actor_email: 'admin@example.com',
  action: 'user_role_changed',
  detail: 'user_id=42 role=driver',
  created_at: '2026-07-20T14:30:00Z',
};

const AUDIT_B = {
  id: 2,
  tenant_name: 'Volt Retail',
  actor_email: 'cpo@example.com',
  action: 'tariff_updated',
  detail: 'tariff_id=99',
  created_at: '2026-07-19T10:15:00Z',
};

const page = (items, total) => ({ total, items });
const tenantsPage = (items, total) => ({ total, items });

const renderPage = () => render(<MemoryRouter><AdminAudit /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('AdminAudit', () => {
  it('lists audit logs with time, organization, actor, action, and detail', async () => {
    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A, TENANT_B], 2));
      return Promise.resolve(page([AUDIT_A, AUDIT_B], 2));
    });
    renderPage();

    await screen.findByText('user_role_changed');
    expect(screen.getByText('admin@example.com')).toBeInTheDocument();
    expect(screen.getByText('cpo@example.com')).toBeInTheDocument();
    expect(screen.getByText('tariff_updated')).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty state on audit fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A], 1));
      return Promise.reject(new Error('down'));
    });
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No audit logs yet')).not.toBeInTheDocument();

    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A], 1));
      return Promise.resolve(page([AUDIT_A], 1));
    });
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('user_role_changed')).toBeInTheDocument();
  });

  it('shows the empty state only for a true zero-row result', async () => {
    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A], 1));
      return Promise.resolve(page([], 0));
    });
    renderPage();
    expect(await screen.findByText('No audit logs yet')).toBeInTheDocument();
  });

  it('loads tenants for the filter dropdown', async () => {
    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A, TENANT_B], 2));
      return Promise.resolve(page([AUDIT_A, AUDIT_B], 2));
    });
    renderPage();

    await screen.findByText('user_role_changed');
    const select = screen.getByLabelText('Filter by organization');
    expect(select).toBeInTheDocument();
    expect(select).toHaveValue('');

    const options = screen.getAllByRole('option');
    expect(options[0]).toHaveTextContent('All organizations');
    expect(options[1]).toHaveTextContent('Acme Charging');
    expect(options[2]).toHaveTextContent('Volt Retail');
  });

  it('filters audit logs by tenant_id when a tenant is selected', async () => {
    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A, TENANT_B], 2));
      if (url.includes('tenant_id=1')) return Promise.resolve(page([AUDIT_A], 1));
      return Promise.resolve(page([AUDIT_A, AUDIT_B], 2));
    });
    renderPage();

    await screen.findByText('user_role_changed');
    api.get.mockClear();
    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A, TENANT_B], 2));
      if (url.includes('tenant_id=1')) return Promise.resolve(page([AUDIT_A], 1));
      return Promise.resolve(page([AUDIT_A, AUDIT_B], 2));
    });

    const select = screen.getByLabelText('Filter by organization');
    await userEvent.selectOptions(select, '1');

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).toContain('tenant_id=1');
    });
  });

  it('pages forward through DataTable pagination', async () => {
    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A], 1));
      return Promise.resolve(page([AUDIT_A], 45));
    });
    renderPage();
    await screen.findByText('user_role_changed');
    api.get.mockClear();

    api.get.mockImplementation((url) => {
      if (url.includes('/api/admin/tenants')) return Promise.resolve(tenantsPage([TENANT_A], 1));
      return Promise.resolve(page([AUDIT_B], 45));
    });

    await userEvent.click(screen.getByRole('button', { name: 'Next' }));

    await waitFor(() => {
      const lastUrl = api.get.mock.calls.at(-1)[0];
      expect(lastUrl).toContain('offset=20');
    });
    expect(await screen.findByText('tariff_updated')).toBeInTheDocument();
  });
});
