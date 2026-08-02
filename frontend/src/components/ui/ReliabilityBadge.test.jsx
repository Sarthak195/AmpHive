/**
 * ReliabilityBadge tests: fetch-on-mount from GET /api/plugs/{id}/reliability,
 * a skeleton while loading, the "NN% online · Nd · seen Xm ago" render on
 * success, and quiet-fail (renders nothing) on a request error OR a null
 * uptime_pct (a plug too young to have a meaningful reading) — mirroring
 * Dashboard's month-stats hide-on-error convention.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import ReliabilityBadge from './ReliabilityBadge';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn() },
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe('ReliabilityBadge', () => {
  it('shows a skeleton before the fetch resolves', () => {
    api.get.mockReturnValue(new Promise(() => {})); // never resolves
    const { container } = render(<ReliabilityBadge plugId={5} />);
    expect(container.querySelector('.skeleton')).toBeInTheDocument();
  });

  it('renders the uptime percentage, window and last-seen once loaded', async () => {
    api.get.mockResolvedValue({
      uptime_pct: 98.4,
      sample_window_days: 7,
      last_seen_at: new Date(Date.now() - 5 * 60_000).toISOString(),
    });
    render(<ReliabilityBadge plugId={5} />);

    expect(await screen.findByText(/98% online/)).toBeInTheDocument();
    expect(screen.getByText(/7d/)).toBeInTheDocument();
    expect(screen.getByText(/seen 5m ago/)).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/plugs/5/reliability');
  });

  it('renders a short-window badge in hours when sample_window_days is under 1', async () => {
    api.get.mockResolvedValue({ uptime_pct: 100, sample_window_days: 0.5, last_seen_at: null });
    render(<ReliabilityBadge plugId={9} />);
    expect(await screen.findByText(/100% online/)).toBeInTheDocument();
    expect(screen.getByText(/12h/)).toBeInTheDocument();
  });

  it('renders nothing when uptime_pct is null (plug too young)', async () => {
    api.get.mockResolvedValue({ uptime_pct: null, sample_window_days: 0.1, last_seen_at: null });
    const { container } = render(<ReliabilityBadge plugId={11} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('renders nothing on a fetch error', async () => {
    api.get.mockRejectedValue(new Error('network down'));
    const { container } = render(<ReliabilityBadge plugId={12} />);
    await waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it('renders nothing when plugId is not given', () => {
    const { container } = render(<ReliabilityBadge />);
    expect(container).toBeEmptyDOMElement();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('re-fetches when plugId changes', async () => {
    api.get.mockResolvedValue({ uptime_pct: 90, sample_window_days: 7, last_seen_at: null });
    const { rerender } = render(<ReliabilityBadge plugId={1} />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/plugs/1/reliability'));

    rerender(<ReliabilityBadge plugId={2} />);
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/plugs/2/reliability'));
  });
});
