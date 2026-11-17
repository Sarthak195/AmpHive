/**
 * SessionPowerCurve tests: fetch + draw, kWh limit marker, empty state,
 * live frame append without a refetch.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

import SessionPowerCurve from './SessionPowerCurve';
import api from '../api/client';

vi.mock('../api/client', () => ({ default: { get: vi.fn() } }));

const DATA = {
  session_id: 5,
  started_at: '2026-07-10T10:00:00Z',
  max_kwh: null,
  points: [
    { offset_sec: 0, power_w: 1000, energy_kwh: 0.01 },
    { offset_sec: 60, power_w: 2000, energy_kwh: 0.05 },
    { offset_sec: 120, power_w: 1500, energy_kwh: 0.1 },
  ],
};

beforeEach(() => { api.get.mockReset(); });

describe('SessionPowerCurve', () => {
  it('fetches once and draws the curve with a limit marker', async () => {
    api.get.mockResolvedValue({ ...DATA, max_kwh: 0.5 });
    const { container } = render(<SessionPowerCurve sessionId={5} />);
    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument());
    expect(api.get).toHaveBeenCalledWith('/api/sessions/5/telemetry');
    expect(container.querySelector('.power-curve-limit')).not.toBeNull();
    expect(screen.getByRole('img').getAttribute('aria-label')).toMatch(/energy limit/);
  });

  it('draws no limit marker when the session has no kWh limit', async () => {
    api.get.mockResolvedValue(DATA);
    const { container } = render(<SessionPowerCurve sessionId={5} />);
    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument());
    expect(container.querySelector('.power-curve-limit')).toBeNull();
  });

  it('shows an empty state when the session has no readings', async () => {
    api.get.mockResolvedValue({ ...DATA, points: [] });
    render(<SessionPowerCurve sessionId={5} />);
    expect(await screen.findByText(/No power readings were recorded/)).toBeInTheDocument();
  });

  it('appends live frames without refetching', async () => {
    api.get.mockResolvedValue({ ...DATA, points: DATA.points.slice(0, 2) });
    const start = new Date(DATA.started_at).getTime();
    const { container, rerender } = render(
      <SessionPowerCurve sessionId={5} live={{ at: start + 61_000, power_w: 2000, energy_kwh: 0.05 }} />,
    );
    await waitFor(() => expect(screen.getByRole('img')).toBeInTheDocument());
    const before = container.querySelector('.power-curve-line').getAttribute('points');
    rerender(
      <SessionPowerCurve sessionId={5} live={{ at: start + 120_000, power_w: 500, energy_kwh: 0.1 }} />,
    );
    await waitFor(() =>
      expect(container.querySelector('.power-curve-line').getAttribute('points')).not.toBe(before));
    expect(api.get).toHaveBeenCalledTimes(1);
  });
});
