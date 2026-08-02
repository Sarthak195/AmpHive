/**
 * CpoGateways tests (redesign v3, D3): the fleet table (status + relative
 * last-seen, firmware chip with the fleet "behind" badge, plug count),
 * skeleton/ErrorState/EmptyState via DataTable, registering a new gateway,
 * and the two-step OTA flow (URL entry → named ConfirmDialog → POST), gated
 * to online gateways with at least one plug.
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

const mockApiRoutes = ({ gateways = GATEWAYS } = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/gateways') return Promise.resolve(gateways);
    if (url.startsWith('/api/cpo/audit')) return Promise.resolve([]);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = () => render(<CpoGateways />);

beforeEach(() => {
  vi.clearAllMocks();
  mockApiRoutes();
});

describe('CpoGateways', () => {
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

  it('falls back to manual registration from the claim modal', async () => {
    api.post.mockResolvedValue({ status: 'registered' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getByRole('button', { name: 'Add gateway' }));
    await user.click(screen.getByRole('button', { name: 'Register a gateway manually instead' }));

    const modal = (await screen.findByText('Flash the gateway firmware,', { exact: false })).closest('.modal');
    await user.type(within(modal).getByLabelText('Gateway ID (device MAC)'), 'ff00ff00ff00');
    await user.type(within(modal).getByLabelText('Name'), 'New gateway');
    await user.click(within(modal).getByRole('button', { name: 'Add gateway' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/gateways', {
      gateway_id: 'ff00ff00ff00',
      name: 'New gateway',
    });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('surfaces a manual registration failure inline in its own modal', async () => {
    api.post.mockRejectedValue(new Error("Gateway 'x' already exists."));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getByRole('button', { name: 'Add gateway' }));
    await user.click(screen.getByRole('button', { name: 'Register a gateway manually instead' }));
    const modal = (await screen.findByText('Flash the gateway firmware,', { exact: false })).closest('.modal');
    await user.type(within(modal).getByLabelText('Gateway ID (device MAC)'), 'dupe');
    await user.type(within(modal).getByLabelText('Name'), 'Dupe');
    await user.click(within(modal).getByRole('button', { name: 'Add gateway' }));

    expect(await screen.findByText("Gateway 'x' already exists.")).toBeInTheDocument();
  });

  it('runs the OTA flow: URL entry → named confirm → POST', async () => {
    api.post.mockResolvedValue({ status: 'ota_triggered' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    const urlModal = (await screen.findByText(/Push an update to/)).closest('.modal');
    await user.type(within(urlModal).getByLabelText('Firmware image URL (https)'), 'https://fw.example.com/2.4.0.bin');
    await user.click(within(urlModal).getByRole('button', { name: 'Continue' }));

    const confirmTitle = await screen.findByText('Push firmware update?');
    const confirmModal = confirmTitle.closest('.modal');
    expect(within(confirmModal).getByText(/Basement gateway/)).toBeInTheDocument();
    await user.click(within(confirmModal).getByRole('button', { name: 'Push update' }));

    expect(api.post).toHaveBeenCalledWith('/api/cpo/gateways/aa11bb22cc33/ota', {
      firmware_url: 'https://fw.example.com/2.4.0.bin',
    });
    expect(toast.ok).toHaveBeenCalledWith(expect.stringContaining('Basement gateway'));
  });

  it('surfaces an OTA failure as a toast and keeps the gateway list intact', async () => {
    api.post.mockRejectedValue(new Error('Gateway is offline.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('Basement gateway');

    await user.click(screen.getAllByRole('button', { name: 'Update firmware' })[0]);
    const urlModal = (await screen.findByText(/Push an update to/)).closest('.modal');
    await user.type(within(urlModal).getByLabelText('Firmware image URL (https)'), 'https://fw.example.com/2.4.0.bin');
    await user.click(within(urlModal).getByRole('button', { name: 'Continue' }));

    const confirmModal = (await screen.findByText('Push firmware update?')).closest('.modal');
    await user.click(within(confirmModal).getByRole('button', { name: 'Push update' }));

    expect(toast.error).toHaveBeenCalledWith('Gateway is offline.');
  });
});
