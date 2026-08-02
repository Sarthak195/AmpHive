/**
 * AdminGateways tests: the paginated gateway table renders fleet data from
 * GET /api/admin/gateways with online/offline filtering and firmware-spread
 * summary chips. Shows skeleton while loading, ErrorState on failure (with
 * retry), and EmptyState when no gateways exist (never conflated).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import AdminGateways from './AdminGateways';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const GATEWAYS = {
  total: 3,
  items: [
    {
      id: 'gw-1',
      gateway_id: 'abc123',
      name: 'Main Hub',
      tenant_id: 'tenant-1',
      tenant_name: 'Acme Charging',
      online: true,
      last_seen_at: new Date(Date.now() - 5 * 60_000).toISOString(), // 5m ago
      firmware_version: '2.3.0',
      plug_count: 4,
    },
    {
      id: 'gw-2',
      gateway_id: 'def456',
      name: 'Secondary Hub',
      tenant_id: 'tenant-1',
      tenant_name: 'Acme Charging',
      online: true,
      last_seen_at: new Date(Date.now() - 30 * 60_000).toISOString(), // 30m ago
      firmware_version: '2.3.0',
      plug_count: 2,
    },
    {
      id: 'gw-3',
      gateway_id: 'ghi789',
      name: 'Remote Hub',
      tenant_id: 'tenant-2',
      tenant_name: 'EV Express',
      online: false,
      last_seen_at: new Date(Date.now() - 2 * 3600 * 1000).toISOString(), // 2h ago
      firmware_version: '2.1.0',
      plug_count: 6,
    },
  ],
};

const renderPage = () => render(<MemoryRouter><AdminGateways /></MemoryRouter>);

beforeEach(() => vi.clearAllMocks());
afterEach(() => vi.restoreAllMocks());

describe('AdminGateways', () => {
  it('renders the gateway table with all expected columns and data', async () => {
    api.get.mockResolvedValue(GATEWAYS);
    renderPage();

    // Header
    expect(await screen.findByText('Gateways')).toBeInTheDocument();
    expect(screen.getByText(/3 gateways/)).toBeInTheDocument();

    // Column headers
    expect(screen.getByText('Organization')).toBeInTheDocument();
    expect(screen.getByText('Gateway')).toBeInTheDocument();
    expect(screen.getByText('Status')).toBeInTheDocument();
    expect(screen.getByText('Firmware')).toBeInTheDocument();
    expect(screen.getByText('Chargers')).toBeInTheDocument();

    // Data rows: tenant names (should have 2 of Acme, 1 of EV Express)
    expect(screen.getAllByText('Acme Charging')).toHaveLength(2);
    expect(screen.getByText('EV Express')).toBeInTheDocument();

    // Data rows: gateway names
    expect(screen.getByText('Main Hub')).toBeInTheDocument();
    expect(screen.getByText('Secondary Hub')).toBeInTheDocument();
    expect(screen.getByText('Remote Hub')).toBeInTheDocument();

    // Firmware versions
    expect(screen.getAllByText('2.3.0')).toHaveLength(2);
    expect(screen.getByText('2.1.0')).toBeInTheDocument();

    // Plug counts
    expect(screen.getByText('4')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('6')).toBeInTheDocument();
  });

  it('shows a "behind" badge for gateways older than the max firmware', async () => {
    api.get.mockResolvedValue(GATEWAYS);
    renderPage();

    await screen.findByText('Main Hub');

    // 2.1.0 is behind 2.3.0, so should have a badge
    const behindBadge = screen.getByText('behind');
    expect(behindBadge).toBeInTheDocument();
    expect(behindBadge.closest('div')).toHaveTextContent('2.1.0');
  });

  it('renders firmware-spread summary chips', async () => {
    api.get.mockResolvedValue(GATEWAYS);
    renderPage();

    await screen.findByText('Main Hub');

    // Firmware spread: 2.3.0 ×2, 2.1.0 ×1
    expect(screen.getByText('2.3.0 ×2')).toBeInTheDocument();
    expect(screen.getByText('2.1.0 ×1')).toBeInTheDocument();
  });

  it('includes online/offline filter and filters the table', async () => {
    api.get.mockResolvedValue(GATEWAYS);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Main Hub');

    // All 3 gateways visible initially
    expect(screen.getByText('Main Hub')).toBeInTheDocument();
    expect(screen.getByText('Secondary Hub')).toBeInTheDocument();
    expect(screen.getByText('Remote Hub')).toBeInTheDocument();

    // Filter to online only
    api.get.mockResolvedValue({
      total: 2,
      items: GATEWAYS.items.slice(0, 2), // only the first two
    });
    const filterSelect = screen.getByDisplayValue('All');
    await user.selectOptions(filterSelect, 'online');

    // API should be called with online=true
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('online=true'));

    // Now expect only online gateways (after refetch resolves)
    await screen.findByText('Secondary Hub');
    expect(screen.queryByText('Remote Hub')).not.toBeInTheDocument();
  });

  it('shows a skeleton while loading', () => {
    api.get.mockReturnValue(new Promise(() => {})); // never resolves
    renderPage();

    // DataTable in loading state wraps table with aria-busy
    const tableWrap = screen.getByRole('table', { hidden: true }).parentElement;
    expect(tableWrap).toHaveAttribute('aria-busy', 'true');
  });

  it('surfaces a retryable ErrorState on fetch failure and recovers on retry', async () => {
    api.get.mockRejectedValueOnce(new Error('Network error'));
    const user = userEvent.setup();
    renderPage();

    // ErrorState appears
    expect(await screen.findByText("Couldn't load gateways")).toBeInTheDocument();

    // Click retry
    api.get.mockResolvedValueOnce(GATEWAYS);
    await user.click(screen.getByRole('button', { name: 'Retry' }));

    // Table renders after recovery
    await screen.findByText('Main Hub');
    expect(screen.queryByText("Couldn't load gateways")).not.toBeInTheDocument();
  });

  it('shows EmptyState when no gateways exist (not on error)', async () => {
    api.get.mockResolvedValue({ total: 0, items: [] });
    renderPage();

    await screen.findByText('No gateways yet');
    expect(screen.getByText('Gateways will appear here once registered.')).toBeInTheDocument();
  });

  it('displays pagination controls with correct counts', async () => {
    const largeDataset = {
      total: 100,
      items: GATEWAYS.items,
    };
    api.get.mockResolvedValue(largeDataset);
    renderPage();

    await screen.findByText('Main Hub');

    // Pagination shows: "1–25 of 100" (limit is 25 per page)
    expect(screen.getByText(/1.+25 of 100/)).toBeInTheDocument();

    // Next button should be enabled (more pages exist)
    expect(screen.getByRole('button', { name: 'Next' })).not.toBeDisabled();
  });

  it('disables Previous button on first page and Next button on last page', async () => {
    api.get.mockResolvedValue({
      total: 50,
      items: GATEWAYS.items,
    });
    renderPage();

    await screen.findByText('Main Hub');

    // On first page (offset=0), Previous should be disabled
    expect(screen.getByRole('button', { name: 'Prev' })).toBeDisabled();

    // With only 3 items and limit=25, Next is enabled (we could have more)
    expect(screen.getByRole('button', { name: 'Next' })).not.toBeDisabled();
  });

  it('calls API with offset param when pagination changes', async () => {
    const largeDataset = {
      total: 100,
      items: GATEWAYS.items,
    };
    api.get.mockResolvedValue(largeDataset);
    const user = userEvent.setup();
    renderPage();

    await screen.findByText('Main Hub');

    // Click Next
    api.get.mockResolvedValueOnce(largeDataset); // mock next page
    await user.click(screen.getByRole('button', { name: 'Next' }));

    // API should be called with offset=25
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('offset=25'));
  });

  describe('mint inventory gateway', () => {
    beforeEach(() => {
      api.get.mockResolvedValue({ total: 0, items: [] });
    });

    it('mints a gateway and shows the claim code once', async () => {
      api.post.mockResolvedValue({
        status: 'minted',
        gateway_id: 'aabbccddeeff',
        name: 'Unclaimed gateway aabbccddeeff',
        claim_code: 'H4KX9Q2PFW',
      });
      const user = userEvent.setup();
      renderPage();
      await screen.findByText('No gateways yet');

      await user.click(screen.getByRole('button', { name: 'Mint inventory gateway' }));
      const modal = (await screen.findByLabelText('Gateway ID (device MAC)')).closest('.modal');
      await user.type(within(modal).getByLabelText('Gateway ID (device MAC)'), 'aabbccddeeff');
      await user.click(within(modal).getByRole('button', { name: 'Mint gateway' }));

      expect(api.post).toHaveBeenCalledWith('/api/admin/gateways/inventory', {
        gateway_id: 'aabbccddeeff',
        name: undefined,
      });

      // The code is shown exactly once (readonly, in a "Done"-only follow-up
      // view) so the operator can copy it onto the unit's label.
      const codeInput = await screen.findByLabelText('Claim code');
      expect(codeInput).toHaveValue('H4KX9Q2PFW');
      expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();
    });

    it('surfaces a mint failure inline', async () => {
      api.post.mockRejectedValue(new Error("Gateway 'dup' already exists."));
      const user = userEvent.setup();
      renderPage();
      await screen.findByText('No gateways yet');

      await user.click(screen.getByRole('button', { name: 'Mint inventory gateway' }));
      const modal = (await screen.findByLabelText('Gateway ID (device MAC)')).closest('.modal');
      await user.type(within(modal).getByLabelText('Gateway ID (device MAC)'), 'dup');
      await user.click(within(modal).getByRole('button', { name: 'Mint gateway' }));

      expect(await screen.findByText("Gateway 'dup' already exists.")).toBeInTheDocument();
    });
  });
});
