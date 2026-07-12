/**
 * NotificationBell tests: feed fetch + unread badge, panel open/mark-read,
 * live socket prepend, and mark-all-read.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import NotificationBell from './NotificationBell';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const mockUseAuth = vi.fn();
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => mockUseAuth(),
}));

const mockUseSession = vi.fn();
vi.mock('../contexts/SessionContext', () => ({
  useSession: () => mockUseSession(),
}));

const FEED = {
  notifications: [
    {
      id: 2, type: 'session_stopped', severity: 'info', title: 'Charging complete',
      body: 'Plug A: 0.500 kWh in 12 min — 2.50 coins charged, 97.50 left.',
      plug_id: 1, session_id: 9, read: false, created_at: '2026-07-11T10:00:00Z',
    },
    {
      id: 1, type: 'topup_credited', severity: 'info', title: 'Wallet topped up',
      body: '100.00 coins credited — balance is now 100.00.',
      plug_id: null, session_id: null, read: true, created_at: '2026-07-11T09:00:00Z',
    },
  ],
  unread_count: 1,
};

// Minimal socket fake: capture listeners so tests can fire events.
const makeSocket = () => {
  const listeners = {};
  return {
    on: vi.fn((ev, fn) => { listeners[ev] = fn; }),
    off: vi.fn((ev) => { delete listeners[ev]; }),
    fire: (ev, data) => listeners[ev] && listeners[ev](data),
  };
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUseAuth.mockReturnValue({ user: { id: 5, email: 'driver@amphive.test' } });
  mockUseSession.mockReturnValue({ socket: null });
  api.get.mockResolvedValue(FEED);
  api.post.mockResolvedValue({ status: 'read' });
});

describe('NotificationBell', () => {
  it('fetches the feed and shows the unread badge', async () => {
    render(<NotificationBell />);
    expect(await screen.findByTestId('unread-badge')).toHaveTextContent('1');
    expect(api.get).toHaveBeenCalledWith('/api/notifications?limit=20');
  });

  it('renders nothing when logged out', () => {
    mockUseAuth.mockReturnValue({ user: null });
    const { container } = render(<NotificationBell />);
    expect(container.firstChild).toBeNull();
    expect(api.get).not.toHaveBeenCalled();
  });

  it('opens the panel and marks an item read on click', async () => {
    render(<NotificationBell />);
    await screen.findByTestId('unread-badge');

    await userEvent.click(screen.getByRole('button', { name: /Notifications/ }));
    const item = await screen.findByText(/Charging complete/);
    await userEvent.click(item);

    expect(api.post).toHaveBeenCalledWith('/api/notifications/2/read', {});
    expect(screen.queryByTestId('unread-badge')).not.toBeInTheDocument();
  });

  it('mark-all-read clears the badge and calls the endpoint', async () => {
    render(<NotificationBell />);
    await screen.findByTestId('unread-badge');

    await userEvent.click(screen.getByRole('button', { name: /Notifications/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Mark all read' }));

    expect(api.post).toHaveBeenCalledWith('/api/notifications/read-all', {});
    expect(screen.queryByTestId('unread-badge')).not.toBeInTheDocument();
  });

  it('prepends live socket notifications and bumps the badge', async () => {
    const socket = makeSocket();
    mockUseSession.mockReturnValue({ socket });
    render(<NotificationBell />);
    await screen.findByTestId('unread-badge');

    act(() => {
      socket.fire('notification', {
        id: 3, type: 'low_balance', severity: 'warning', title: 'Balance running low',
        body: '~10.00 coins left.', read: false, created_at: '2026-07-11T10:05:00Z',
      });
    });

    expect(screen.getByTestId('unread-badge')).toHaveTextContent('2');
    await userEvent.click(screen.getByRole('button', { name: /Notifications/ }));
    expect(await screen.findByText(/Balance running low/)).toBeInTheDocument();
  });
});
