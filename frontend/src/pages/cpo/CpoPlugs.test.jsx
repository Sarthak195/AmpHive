/**
 * CpoPlugs QR-code modal tests: the per-plug "QR" action renders a QR code
 * pointed at the driver deep-link start (`/?plug=<id>`), built from
 * window.location.origin at render time (never hardcoded), with a Print
 * action and a Close action.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoPlugs from './CpoPlugs';
import api from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));
vi.mock('../../contexts/AuthContext', () => ({ useAuth: vi.fn() }));

const PLUGS = [
  {
    id: 5, name: 'Lobby Plug', gateway_id: 'gw-1', local_ip: '192.168.1.5',
    group_id: null, group_name: null, status: 'available', current_power_w: 0,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  useAuth.mockReturnValue({ user: { email: 'cpo@amphive.test', role: 'cpo', full_name: 'CPO Operator' } });
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/plugs') return Promise.resolve(PLUGS);
    if (url === '/api/cpo/gateways') return Promise.resolve([]);
    if (url === '/api/cpo/groups') return Promise.resolve([]);
    if (url === '/api/cpo/profile') return Promise.resolve({ tenant: { name: 'Test CPO' } });
    return Promise.resolve([]);
  });
});

afterEach(() => {
  delete window.print;
});

const renderPlugs = () =>
  render(
    <MemoryRouter>
      <CpoPlugs />
    </MemoryRouter>
  );

describe('CpoPlugs — QR code action', () => {
  it('opens a modal with the plug name/id and the deep-link start URL, read from window.location.origin', async () => {
    renderPlugs();
    await userEvent.click(await screen.findByRole('button', { name: /QR/ }));

    // "Lobby Plug" now appears twice (table row + modal) — the modal-only
    // details are what prove the QR action wired up the right plug/URL.
    expect(screen.getAllByText('Lobby Plug').length).toBeGreaterThan(1);
    expect(screen.getByText('Plug ID: 5')).toBeInTheDocument();
    expect(screen.getByText(`${window.location.origin}/?plug=5`)).toBeInTheDocument();
  });

  it('prints via window.print when Print is clicked', async () => {
    window.print = vi.fn();
    renderPlugs();
    await userEvent.click(await screen.findByRole('button', { name: /QR/ }));
    await userEvent.click(screen.getByRole('button', { name: /Print/ }));
    expect(window.print).toHaveBeenCalled();
  });

  it('closes via the Close button', async () => {
    renderPlugs();
    await userEvent.click(await screen.findByRole('button', { name: /QR/ }));
    expect(screen.getByText('QR Code')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByText('QR Code')).not.toBeInTheDocument();
  });
});
