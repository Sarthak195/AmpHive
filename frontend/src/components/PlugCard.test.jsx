/**
 * PlugCard tests — the explicit five-state action machine:
 * available → Charge + Reserve (with reserved-for-you / reserved-by-other
 * variants), in_use → Notify-me bell + Reserve, unpowered → Queue charge only
 * when the payload advertises queue_available (else the bell) with its
 * sublabel, offline / maintenance → no actions. Plus price + next-price meta.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import PlugCard from './PlugCard';

vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coins_per_kwh: 5, coin_inr_rate: 1 }),
}));

const BASE = {
  id: 1,
  name: 'Lobby Plug',
  status: 'available',
  gateway_online: true,
  plug_powered: true,
  price_per_kwh: 8,
  group_name: 'Sunrise Apartments',
};

const handlers = {
  onCharge: vi.fn(),
  onReserve: vi.fn(),
  onQueue: vi.fn(),
  onToggleWatch: vi.fn(),
};

const renderCard = (plug) => render(<PlugCard plug={plug} {...handlers} />);

beforeEach(() => vi.clearAllMocks());

describe('PlugCard — available', () => {
  it('shows Charge + Reserve and wires both callbacks', async () => {
    renderCard(BASE);
    expect(screen.getByText('Available')).toBeInTheDocument();
    expect(screen.getByText('Sunrise Apartments')).toBeInTheDocument();
    expect(screen.getByText(/₹8\.00/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /charge/i }));
    expect(handlers.onCharge).toHaveBeenCalledWith(BASE);
    await userEvent.click(screen.getByRole('button', { name: /reserve/i }));
    expect(handlers.onReserve).toHaveBeenCalledWith(BASE);
  });

  it('previews the next time-of-day price when it differs', () => {
    renderCard({
      ...BASE,
      price_next_per_kwh: 6,
      price_changes_at: '2030-01-01T22:00:00+05:30',
    });
    expect(screen.getByText(/₹6\.00/)).toBeInTheDocument();
    expect(screen.getByText(/after/)).toBeInTheDocument();
  });

  it("someone else's covering reservation removes Charge and shows the badge", () => {
    renderCard({
      ...BASE,
      reserved_now: true,
      reserved_now_by_me: false,
      reserved_until: '2030-01-01T22:00:00Z',
    });
    expect(screen.queryByRole('button', { name: /^charge/i })).not.toBeInTheDocument();
    expect(screen.getByText(/Reserved until/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /reserve/i })).toBeInTheDocument();
  });

  it('the holder sees "Reserved for you" and Charge stays enabled', () => {
    renderCard({ ...BASE, reserved_now: true, reserved_now_by_me: true });
    expect(screen.getByText('Reserved for you')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /charge/i })).toBeEnabled();
  });
});

describe('PlugCard — in use', () => {
  const IN_USE = { ...BASE, status: 'occupied' };

  it('shows the Notify-me bell toggle (aria-pressed) and Reserve, no Charge', async () => {
    renderCard(IN_USE);
    expect(screen.getByText('In use')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^charge/i })).not.toBeInTheDocument();

    const bell = screen.getByRole('button', { name: /notify me when/i });
    expect(bell).toHaveAttribute('aria-pressed', 'false');
    await userEvent.click(bell);
    expect(handlers.onToggleWatch).toHaveBeenCalledWith(IN_USE);

    expect(screen.getByRole('button', { name: /reserve/i })).toBeInTheDocument();
  });

  it('an armed watch renders as pressed "Watching"', () => {
    renderCard({ ...IN_USE, watching: true });
    const bell = screen.getByRole('button', { name: /stop watching/i });
    expect(bell).toHaveAttribute('aria-pressed', 'true');
    expect(bell).toHaveTextContent('Watching');
  });
});

describe('PlugCard — unpowered', () => {
  const UNPOWERED = { ...BASE, plug_powered: false };

  it('offers Queue charge only when the payload advertises queue_available', async () => {
    renderCard({ ...UNPOWERED, queue_available: true });
    expect(screen.getByText('No mains power right now')).toBeInTheDocument();
    const queueBtn = screen.getByRole('button', { name: /queue charge/i });
    await userEvent.click(queueBtn);
    expect(handlers.onQueue).toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: /notify me/i })).not.toBeInTheDocument();
  });

  it('falls back to the Notify-me bell when queueing is not available', () => {
    renderCard({ ...UNPOWERED, queue_available: false });
    expect(screen.queryByRole('button', { name: /queue charge/i })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /notify me when/i })).toBeInTheDocument();
  });
});

describe('PlugCard — offline and maintenance', () => {
  it('offline: no action buttons, "Can\'t be reached" sublabel', () => {
    renderCard({ ...BASE, gateway_online: false });
    expect(screen.getByText("Can't be reached right now")).toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });

  it('maintenance: no action buttons, "Under maintenance" badge', () => {
    renderCard({ ...BASE, status: 'maintenance' });
    expect(screen.getAllByText('Under maintenance').length).toBeGreaterThan(0);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
  });
});
