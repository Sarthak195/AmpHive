/**
 * PlugCard tests — the explicit five-state action machine:
 * available → Charge + Reserve (with reserved-for-you / reserved-by-other
 * variants), in_use → Notify-me bell + Reserve, unpowered → Queue charge only
 * when the payload advertises queue_available (else the bell) with its
 * sublabel, offline / maintenance → no state actions. Plus price + next-price
 * meta, and the "Report" flag action present in every state (including
 * offline/maintenance — those are exactly the plugs most worth reporting).
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
  onReport: vi.fn(),
  onToggleFavorite: vi.fn(),
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
  it('offline: only the bookmark star and Report remain, "Can't be reached" sublabel', () => {
    renderCard({ ...BASE, gateway_online: false });
    expect(screen.getByText("Can't be reached right now")).toBeInTheDocument();
    // The favorite star is a bookmark and Report must work on broken
    // chargers -- both stay available in every state. No state actions render.
    const buttons = screen.queryAllByRole('button');
    expect(buttons).toHaveLength(2);
    expect(screen.getByRole('button', { name: /favorites/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /report/i })).toBeInTheDocument();
  });

  it('maintenance: only the bookmark star and Report remain, "Under maintenance" badge', () => {
    renderCard({ ...BASE, status: 'maintenance' });
    expect(screen.getAllByText('Under maintenance').length).toBeGreaterThan(0);
    const buttons = screen.queryAllByRole('button');
    expect(buttons).toHaveLength(2);
    expect(screen.getByRole('button', { name: /favorites/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /report/i })).toBeInTheDocument();
  });
});

describe('PlugCard — Report action', () => {
  it('renders a Report action in every state and wires onReport', async () => {
    renderCard(BASE);
    const reportBtn = screen.getByRole('button', { name: /report/i });
    expect(reportBtn).toBeInTheDocument();
    await userEvent.click(reportBtn);
    expect(handlers.onReport).toHaveBeenCalledWith(BASE);
  });
});

describe('PlugCard — favorite star', () => {
  it('renders unfilled with aria-pressed=false and calls onToggleFavorite', async () => {
    renderCard(BASE);
    const star = screen.getByRole('button', { name: 'Add Lobby Plug to favorites' });
    expect(star).toHaveAttribute('aria-pressed', 'false');
    await userEvent.click(star);
    expect(handlers.onToggleFavorite).toHaveBeenCalledWith(BASE);
  });

  it('a favorited plug renders the star pressed with remove copy', () => {
    renderCard({ ...BASE, is_favorite: true });
    const star = screen.getByRole('button', { name: 'Remove Lobby Plug from favorites' });
    expect(star).toHaveAttribute('aria-pressed', 'true');
  });

  it('omits the star when no onToggleFavorite handler is given (Report still renders)', () => {
    render(<PlugCard plug={{ ...BASE, gateway_online: false }} />);
    expect(screen.queryByRole('button', { name: /favorites/i })).not.toBeInTheDocument();
    expect(screen.queryAllByRole('button')).toHaveLength(1);
  });

  it('shows rated power and connector chips when the specs are set', () => {
    renderCard({ ...BASE, rated_power_w: 3300, connector_type: 'Type 2' });
    expect(screen.getByText('3.3 kW')).toBeInTheDocument();
    expect(screen.getByText('Type 2')).toBeInTheDocument();
  });
});
