/**
 * AdminHealth tests: gauges render from GET /api/admin/health, null sources
 * read "unknown" (never a fake zero), and a failed load shows a retryable error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import AdminHealth from './AdminHealth';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const HEALTH = {
  memory: { total_mb: 1000, available_mb: 200, swap_total_mb: 512, swap_used_mb: 128 },
  cgroup: { current_mb: 300, limit_mb: null },
  disk: { total_gb: 20, free_gb: 12.5, used_pct: 37.5 },
  backend_rss_mb: 180,
  host_uptime_s: 90000,
  process_uptime_s: 3700,
  database: { size_mb: 52, connections: 7 },
  last_runs: { session_reaper: null, telemetry_flush: null },
  generated_at: '2026-11-17T00:00:00Z',
};

beforeEach(() => vi.clearAllMocks());

describe('AdminHealth', () => {
  it('renders gauges from the health snapshot', async () => {
    api.get.mockResolvedValue(HEALTH);
    render(<AdminHealth />);
    expect(await screen.findByText('200 MB free')).toBeInTheDocument();
    expect(screen.getByText('800 MB used of 1000 MB')).toBeInTheDocument();
    expect(screen.getByText('12.5 GB free')).toBeInTheDocument();
    expect(screen.getByText('7 connections')).toBeInTheDocument();
    expect(screen.getByText('No limit set')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/admin/health');
  });

  it('shows unknown for sources the host could not read', async () => {
    api.get.mockResolvedValue({ ...HEALTH, memory: null, cgroup: null, disk: null, backend_rss_mb: null });
    render(<AdminHealth />);
    await screen.findByText('Host memory');
    expect(screen.getAllByText('unknown').length).toBeGreaterThanOrEqual(4);
    expect(screen.queryByText('0 MB')).not.toBeInTheDocument();
  });

  it('shows a retryable error when the first load fails', async () => {
    api.get.mockRejectedValueOnce(new Error('boom')).mockResolvedValue(HEALTH);
    render(<AdminHealth />);
    await userEvent.click(await screen.findByRole('button', { name: /retry|try again/i }));
    expect(await screen.findByText('200 MB free')).toBeInTheDocument();
  });
});
