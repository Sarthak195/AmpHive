/**
 * SessionPowerCurve tests: fetch + draw, kWh limit marker, empty and failed
 * states, real peak + average current, live frame append, the 60 s live
 * refetch and the one-shot post-end retry.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';

import SessionPowerCurve from './SessionPowerCurve';
import api from '../api/client';

vi.mock('../api/client', () => ({ default: { get: vi.fn() } }));

const DATA = {
  session_id: 5,
  started_at: '2026-07-10T10:00:00Z',
  max_kwh: null,
  bucket_seconds: 60,
  points: [
    { offset_sec: 0, power_w: 1000, peak_power_w: 1100, current_a: 4, energy_kwh: 0.01 },
    { offset_sec: 60, power_w: 2000, peak_power_w: 2400, current_a: 8, energy_kwh: 0.05 },
    { offset_sec: 120, power_w: 1500, peak_power_w: 1600, current_a: 6, energy_kwh: 0.1 },
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

  it('captions the real peak (bucket maxima) and the average current', async () => {
    api.get.mockResolvedValue(DATA);
    render(<SessionPowerCurve sessionId={5} />);
    const img = await screen.findByRole('img');
    // Peak is the max of peak_power_w (2.4 kW), not of the averages (2.0 kW).
    expect(img.getAttribute('aria-label')).toMatch(/peak 2\.4 kW/);
    expect(img.getAttribute('aria-label')).toMatch(/average current 6\.0 A/);
    expect(screen.getByText(/^Avg/).textContent).toMatch(/6\.0 A/);
  });

  it('shows a distinct unavailable state when the fetch fails', async () => {
    api.get.mockRejectedValue(new Error('boom'));
    render(<SessionPowerCurve sessionId={5} />);
    expect(await screen.findByText('Power curve unavailable')).toBeInTheDocument();
    expect(screen.queryByText(/No power readings were recorded/)).toBeNull();
  });

  describe('timers', () => {
    beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }); });
    afterEach(() => { vi.useRealTimers(); });

    it('refetches a live session every 60 s and retries after a failure', async () => {
      const start = new Date(DATA.started_at).getTime();
      api.get.mockRejectedValueOnce(new Error('boom')).mockResolvedValue(DATA);
      render(<SessionPowerCurve sessionId={5} live={{ at: start + 30_000, power_w: 900 }} />);
      expect(await screen.findByText('Power curve unavailable')).toBeInTheDocument();
      expect(api.get).toHaveBeenCalledTimes(1);

      await act(async () => { vi.advanceTimersByTime(60_000); });
      expect(await screen.findByRole('img')).toBeInTheDocument();
      expect(api.get).toHaveBeenCalledTimes(2);

      await act(async () => { vi.advanceTimersByTime(60_000); });
      expect(api.get).toHaveBeenCalledTimes(3);
    });

    it('does not poll a finished session', async () => {
      api.get.mockResolvedValue(DATA);
      render(<SessionPowerCurve sessionId={5} />);
      await screen.findByRole('img');
      await act(async () => { vi.advanceTimersByTime(180_000); });
      expect(api.get).toHaveBeenCalledTimes(1);
    });

    it('retries once ~12 s after a session that just ended with a short curve', async () => {
      const endedAt = new Date(Date.now() - 3_000).toISOString();
      const started = new Date(Date.now() - 303_000).toISOString();
      const short = { ...DATA, started_at: started, points: DATA.points.slice(0, 2) };
      const full = { ...DATA, started_at: started, points: [
        ...DATA.points,
        { offset_sec: 240, power_w: 1200, peak_power_w: 1300, current_a: 5, energy_kwh: 0.2 },
        { offset_sec: 300, power_w: 1100, peak_power_w: 1200, current_a: 5, energy_kwh: 0.25 },
      ] };
      api.get.mockResolvedValueOnce(short).mockResolvedValue(full);
      render(<SessionPowerCurve sessionId={5} endedAt={endedAt} />);
      await screen.findByRole('img');
      expect(api.get).toHaveBeenCalledTimes(1);

      await act(async () => { vi.advanceTimersByTime(12_000); });
      await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2));
      await act(async () => { vi.advanceTimersByTime(60_000); });
      expect(api.get).toHaveBeenCalledTimes(2); // once, never again
    });

    it('does not retry a session that ended long ago', async () => {
      const endedAt = new Date(Date.now() - 600_000).toISOString();
      api.get.mockResolvedValue({ ...DATA, points: [] });
      render(<SessionPowerCurve sessionId={5} endedAt={endedAt} />);
      await screen.findByText(/No power readings were recorded/);
      await act(async () => { vi.advanceTimersByTime(30_000); });
      expect(api.get).toHaveBeenCalledTimes(1);
    });
  });
});
