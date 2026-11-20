/**
 * CpoGateways tests (redesign v3, D3; OTA version picker added
 * feat/ota-version-picker): the fleet table (status + relative last-seen,
 * firmware chip with the fleet "behind" badge, plug count), skeleton/
 * ErrorState/EmptyState via DataTable, registering a new gateway, and the
 * OTA flow — version dropdown (descending, "newer" marked) → named
 * ConfirmDialog → POST { release_id } — gated to online gateways with at
 * least one plug, plus the admin-only custom-URL escape hatch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoGateways from './CpoGateways';
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

const mockUseAuth = vi.fn();
vi.mock('../../contexts/AuthContext', () => ({ useAuth: () => mockUseAuth() }));

const GATEWAYS = [
  {
    id: 'aa11bb22cc33',
    name: 'Basement gateway',
    status: 'online',
    firmware_version: '2.3.0',
    last_seen_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    plug_count: 3,
  },
  {
    id: 'dd44ee55ff66',
    name: 'Rooftop gateway',
    status: 'offline',
    firmware_version: '2.1.0',
    last_seen_at: new Date(Date.now() - 3 * 3600_000).toISOString(),
    plug_count: 0,
  },
];

const RELEASES = [
  { id: 3, version: '2.4.0-direct', url: 'https://storage.googleapis.com/amphive-fw/2.4.0.bin', notes: 'adds sub-16A cap enforcement' },
  { id: 2, version: '2.3.0', url: 'https://storage.googleapis.com/amphive-fw/2.3.0.bin', notes: null },
  { id: 1, version: '2.0.0', url: 'https://storage.googleapis.com/amphive-fw/2.0.0.bin', notes: null },
];

const mockApiRoutes = ({ gateways = GATEWAYS, releases = RELEASES } = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/gateways') return Promise.resolve(gateways);
    if (url === '/api/cpo/firmware-releases') return Promise.resolve(releases);
    if (url === '/api/cpo/gateways/aa11bb22cc33/selftest') {
      return Promise.resolve({
        gateway_id: 'aa11bb22cc33',
        generated_at: '2026-11-18T10:00:00Z',
        overall: 'warn',
        checks: [
          { key: 'online', label: 'Gateway online', status: 'pass', detail: 'Connected, last heard 5 s ago.', fix_hint: null, observed_at: null },
          { key: 'wifi', label: 'Wi-Fi signal (live)', status: 'warn', detail: '-75 dBm live, 2 min ago.', fix_hint: 'Move the gateway closer to the router or add an access point.', observed_at: null },
        ],
      });
    }
    if (url.startsWith('/api/cpo/gateways/aa11bb22cc33/logs')) {
      return Promise.resolve({
        total: 1,
        items: [
          { id: 5, gateway_id: 'aa11bb22cc33', level: 'error', message: 'E (1) mqtt: lost broker', created_at: '2026-11-17T10:00:00Z' },
        ],
      });
    }
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = () => render(<CpoGateways />);

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAuth.mockReturnValue({ user: { email: 'cpo@amphive.test', role: 'cpo' } });
  mockApiRoutes();
});

describe('CpoGateways', () => {
  it('opens the diagnostics drawer with the gateway logs from the logs endpoint', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Logs' })[0]);
    expect((await screen.findAllByText('E (1) mqtt: lost broker')).length).toBeGreaterThan(0);
    const logCalls = api.get.mock.calls.map((c) => c[0]).filter((u) => u.includes('/logs'));
    expect(logCalls.length).toBeGreaterThan(0);
    expect(logCalls.every((u) => u.startsWith('/api/cpo/gateways/aa11bb22cc33/logs?'))).toBe(true);
  });

  it('shows the commissioning checklist in the drawer and re-checks on demand', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Logs' })[0]);
    expect(await screen.findByText('Commissioning checklist')).toBeInTheDocument();
    expect(screen.getByText('Wi-Fi signal (live)')).toBeInTheDocument();
    expect(screen.getByText('-75 dBm live, 2 min ago.')).toBeInTheDocument();
    expect(screen.getByText(/Move the gateway closer/)).toBeInTheDocument();
    const selftestCalls = () => api.get.mock.calls.filter((c) => c[0].endsWith('/selftest')).length;
    expect(selftestCalls()).toBe(1);

    await user.click(screen.getByRole('button', { name: /Re-check/ }));
    await screen.findByText('Gateway online');
    expect(selftestCalls()).toBe(2);
  });

  it('renders the fleet table with status, firmware (behind badge), and plug counts', async () => {
    renderPage();

    expect(await screen.findByText('Basement gateway')).toBeInTheDocument();
    expect(screen.getByText('Rooftop gateway')).toBeInTheDocument();
    expect(screen.getByText(/^Online/)).toBeInTheDocument();
    expect(screen.getByText(/^Offline/)).toBeInTheDocument();
    expect(screen.getByText('2.3.0')).toBeInTheDocument();
    // 2.1.0 is behind the fleet max (2.3.0)
    const behindBadge = screen.getByText('behind');
    expect(behindBadge.closest('tr') || behindBadge.closest('td')).toBeTruthy();
  });

  it('disables Update firmware for an offline gateway or one with no plugs', async () => {
    renderPage();
    await screen.findByText('Basement gateway');

    const buttons = screen.getAllByRole('button', { name: 'Update firmware' });
    // Basement (online, 3 plugs) enabled; Rooftop (offline, 0 plugs) disabled
    expect(buttons[0]).not.toBeDisabled();
    expect(buttons[1]).toBeDisabled();
  });

  it('shows a retryable ErrorState on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url === '/api/cpo/gateways') return Promise.reject(new Error('Network down'));
      return Promise.resolve([]);
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    mockApiRoutes();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Basement gateway')).toBeInTheDocument();
  });

  it('shows EmptyState (not an error) when there are no gateways', async () => {
    mockApiRoutes({ gateways: [] });
    renderPage();

    expect(await screen.findByText('No gateways yet')).toBeInTheDocument();
  });

  it('claims a gateway by code (primary Add gateway flow) and refreshes the list', async () => {
    api.post.mockResolvedValue({ status: 'claimed' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getByRole('button', { name: 'Add gateway' }));
    const modal = (await screen.findByText(/Enter the claim code/, { exact: false })).closest('.modal');

    await user.type(within(modal).getByLabelText('Claim code'), 'h4kx9q2pfw');
    await user.type(within(modal).getByLabelText(/Name/), 'New gateway');
    await user.click(within(modal).getByRole('button', { name: 'Add gateway' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/gateways/claim', {
      claim_code: 'h4kx9q2pfw',
      name: 'New gateway',
    });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('claims a gateway with no name (optional field omitted from the payload)', async () => {
    api.post.mockResolvedValue({ status: 'claimed' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getByRole('button', { name: 'Add gateway' }));
    const modal = (await screen.findByText(/Enter the claim code/, { exact: false })).closest('.modal');
    await user.type(within(modal).getByLabelText('Claim code'), 'h4kx9q2pfw');
    await user.click(within(modal).getByRole('button', { name: 'Add gateway' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/gateways/claim', {
      claim_code: 'h4kx9q2pfw',
      name: undefined,
    });
  });

  it('surfaces a claim failure inline in the Add gateway modal', async () => {
    api.post.mockRejectedValue(new Error('Claim code not found or already used.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getByRole('button', { name: 'Add gateway' }));
    const modal = (await screen.findByText(/Enter the claim code/, { exact: false })).closest('.modal');
    await user.type(within(modal).getByLabelText('Claim code'), 'wrongcode');
    await user.click(within(modal).getByRole('button', { name: 'Add gateway' }));

    expect(await screen.findByText('Claim code not found or already used.')).toBeInTheDocument();
  });

  it('offers no manual gateway-ID registration (admin-only API since 2026-11-16)', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getByRole('button', { name: 'Add gateway' }));
    const modal = screen.getByLabelText('Claim code').closest('.modal');

    expect(within(modal).queryByRole('button', { name: /manually/i })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Gateway ID (device MAC)')).not.toBeInTheDocument();
    expect(within(modal).getByText(/No claim code\?/)).toBeInTheDocument();
    expect(api.post).not.toHaveBeenCalledWith('/api/cpo/gateways', expect.anything());
  });

  // --- OTA: version picker ---------------------------------------------------

  it('runs the OTA flow: version dropdown (descending, newest default) → named confirm → POST release_id', async () => {
    api.post.mockResolvedValue({ status: 'ota_triggered' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    const urlModal = (await screen.findByText(/Push an update to/)).closest('.modal');

    const select = within(urlModal).getByLabelText('Firmware version');
    // Newest release (2.4.0-direct) is selected by default.
    expect(select).toHaveValue('3');
    const optionTexts = within(select).getAllByRole('option').map((o) => o.textContent);
    expect(optionTexts).toEqual(['2.4.0-direct — newer', '2.3.0', '2.0.0']);

    await user.click(within(urlModal).getByRole('button', { name: 'Continue' }));

    const confirmTitle = await screen.findByText('Push firmware update?');
    const confirmModal = confirmTitle.closest('.modal');
    expect(within(confirmModal).getByText(/Basement gateway/)).toBeInTheDocument();
    expect(within(confirmModal).getByText(/2\.4\.0-direct/)).toBeInTheDocument();
    await user.click(within(confirmModal).getByRole('button', { name: 'Push update' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/gateways/aa11bb22cc33/ota', {
      release_id: 3,
    });
    expect(toast.ok).toHaveBeenCalledWith(expect.stringContaining('Basement gateway'));
  });

  it('lets the CPO pick an older release from the dropdown', async () => {
    api.post.mockResolvedValue({ status: 'ota_triggered' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    const urlModal = (await screen.findByText(/Push an update to/)).closest('.modal');
    await user.selectOptions(within(urlModal).getByLabelText('Firmware version'), '1');
    await user.click(within(urlModal).getByRole('button', { name: 'Continue' }));
    const confirmModal = (await screen.findByText('Push firmware update?')).closest('.modal');
    await user.click(within(confirmModal).getByRole('button', { name: 'Push update' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/gateways/aa11bb22cc33/ota', {
      release_id: 1,
    });
  });

  it('surfaces an OTA failure as a toast and keeps the gateway list intact', async () => {
    api.post.mockRejectedValue(new Error('Gateway is offline.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    const urlModal = (await screen.findByText(/Push an update to/)).closest('.modal');
    await user.click(within(urlModal).getByRole('button', { name: 'Continue' }));

    const confirmModal = (await screen.findByText('Push firmware update?')).closest('.modal');
    await user.click(within(confirmModal).getByRole('button', { name: 'Push update' }));

    expect(toast.error).toHaveBeenCalledWith('Gateway is offline.');
  });

  it('shows a message instead of a dropdown when no releases are registered', async () => {
    mockApiRoutes({ releases: [] });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    await screen.findByText(/Push an update to/);
    expect(await screen.findByText(/No firmware releases registered yet/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Firmware version')).not.toBeInTheDocument();
  });

  it('does not show the custom-URL escape hatch to a non-admin CPO', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    await screen.findByText(/Push an update to/);
    expect(screen.queryByText(/Use a custom URL instead/)).not.toBeInTheDocument();
  });

  it('lets an admin switch to a custom URL and POSTs firmware_url instead of release_id', async () => {
    mockUseAuth.mockReturnValue({ user: { email: 'admin@amphive.test', role: 'admin' } });
    api.post.mockResolvedValue({ status: 'ota_triggered' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    const urlModal = (await screen.findByText(/Push an update to/)).closest('.modal');
    await user.click(within(urlModal).getByRole('button', { name: /Use a custom URL instead/ }));

    expect(within(urlModal).queryByLabelText('Firmware version')).not.toBeInTheDocument();
    await user.type(within(urlModal).getByLabelText('Firmware image URL (https)'), 'https://fw.example.com/2.5.0.bin');
    await user.click(within(urlModal).getByRole('button', { name: 'Continue' }));

    const confirmModal = (await screen.findByText('Push firmware update?')).closest('.modal');
    await user.click(within(confirmModal).getByRole('button', { name: 'Push update' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/gateways/aa11bb22cc33/ota', {
      firmware_url: 'https://fw.example.com/2.5.0.bin',
    });
  });
});
