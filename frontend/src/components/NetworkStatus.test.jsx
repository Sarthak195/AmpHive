import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import NetworkStatus from './NetworkStatus';
import api from '../api/client';

vi.mock('../api/client', () => ({ default: { get: vi.fn() } }));

const PAYLOAD = {
  status: 'degraded',
  db: 'ok',
  gateways: { online: 0, total: 1, last_seen_at: null },
  plugs: { total: 2, available: 1, occupied: 0, offline: 1, maintenance: 0 },
  reachability: [{ plug_id: 1, name: 'Alpha', status: 'available', online: true, uptime_7d_pct: 99.5 }],
  tariff_today: { timezone: 'Asia/Kolkata', hourly: Array(24).fill(6) },
};

describe('NetworkStatus', () => {
  beforeEach(() => {
    api.get.mockReset();
  });

  it('does not fetch until opened, then renders the public status', async () => {
    api.get.mockResolvedValue(PAYLOAD);
    render(<NetworkStatus />);
    expect(api.get).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole('button', { name: 'Network status' }));

    expect(await screen.findByText('Some chargers unreachable')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/status');
    expect(screen.getByText('Gateways online: 0/1')).toBeInTheDocument();
    expect(screen.getByText('99.5% reachable, 7 d')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: /Hourly price today/ })).toBeInTheDocument();
  });

  it('shows a quiet message when the status call fails', async () => {
    api.get.mockRejectedValue(new Error('boom'));
    render(<NetworkStatus />);
    await userEvent.click(screen.getByRole('button', { name: 'Network status' }));
    expect(await screen.findByText('Status unavailable right now.')).toBeInTheDocument();
  });
});
