/**
 * Account tests: read-only profile fields (member-since only when the API
 * provides it — no fake data), the password-reset trigger, the web-push
 * enable/disable/unsupported/denied states (moved here from
 * NotificationBell), and the driver-only "Host your chargers" card.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import Account from './Account';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../components/ui', () => ({ useToast: () => toast }));

vi.mock('../utils/appHost', () => ({ cpoOrigin: () => 'https://cpo.amphive.app' }));

let mockUser;
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: mockUser }),
}));

const baseUser = {
  email: 'driver@amphive.test',
  full_name: 'Driver One',
  role: 'driver',
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { ...baseUser };
});

describe('profile card', () => {
  it('shows name and email', () => {
    render(<Account />);
    expect(screen.getByText('Driver One')).toBeInTheDocument();
    expect(screen.getByText('driver@amphive.test')).toBeInTheDocument();
  });

  it('omits member-since when the API does not provide created_at', () => {
    render(<Account />);
    expect(screen.queryByText('Member since')).not.toBeInTheDocument();
  });

  it('shows member-since when created_at is present', () => {
    mockUser = { ...baseUser, created_at: '2026-01-15T00:00:00Z' };
    render(<Account />);
    expect(screen.getByText('Member since')).toBeInTheDocument();
    expect(screen.getByText('January 2026')).toBeInTheDocument();
  });
});

describe('security card', () => {
  it('sends the reset email to the signed-in address and confirms', async () => {
    api.post.mockResolvedValue({});
    render(<Account />);

    await userEvent.click(screen.getByRole('button', { name: 'Reset your password' }));

    expect(api.post).toHaveBeenCalledWith('/api/auth/forgot-password', {
      email: 'driver@amphive.test',
    });
    expect(await screen.findByText(/Check your inbox/)).toBeInTheDocument();
    expect(toast.ok).toHaveBeenCalled();
  });

  it('surfaces failures via toast instead of a silent no-op', async () => {
    api.post.mockRejectedValue(new Error('Too many requests. Try again later.'));
    render(<Account />);

    await userEvent.click(screen.getByRole('button', { name: 'Reset your password' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Too many requests. Try again later.')
    );
    expect(screen.queryByText(/Check your inbox/)).not.toBeInTheDocument();
  });
});

describe('notifications card — push unsupported (default jsdom environment)', () => {
  it('shows an unsupported message with no toggle button', async () => {
    render(<Account />);
    expect(
      await screen.findByText(/aren.t supported in this browser/)
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /push notifications/i })).not.toBeInTheDocument();
  });
});

describe('notifications card — push supported', () => {
  let subscription;
  let registration;

  beforeEach(() => {
    subscription = {
      endpoint: 'https://push.example/abc',
      toJSON: () => ({ endpoint: 'https://push.example/abc' }),
      unsubscribe: vi.fn().mockResolvedValue(true),
    };
    registration = {
      pushManager: {
        getSubscription: vi.fn().mockResolvedValue(null),
        subscribe: vi.fn().mockResolvedValue(subscription),
      },
    };

    vi.stubGlobal('PushManager', function PushManagerStub() {});
    vi.stubGlobal('Notification', {
      permission: 'default',
      requestPermission: vi.fn().mockResolvedValue('granted'),
    });
    Object.defineProperty(window.navigator, 'serviceWorker', {
      configurable: true,
      value: {
        register: vi.fn().mockResolvedValue(registration),
        getRegistration: vi.fn().mockResolvedValue(registration),
      },
    });

    api.get.mockResolvedValue({ enabled: true, vapid_public_key: 'AAAA' });
    // The security-card tests above leave api.post's implementation rejected
    // (clearAllMocks resets calls, not implementations) — restore a
    // resolving default for the push-subscribe calls in this block.
    api.post.mockResolvedValue({});
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    delete window.navigator.serviceWorker;
  });

  it('enables push and subscribes with the fetched VAPID key', async () => {
    render(<Account />);
    const button = await screen.findByRole('button', { name: 'Enable push notifications' });
    await userEvent.click(button);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/notifications/push/subscribe', {
        endpoint: 'https://push.example/abc',
      })
    );
    expect(await screen.findByText('Push notifications are on.')).toBeInTheDocument();
  });

  it('disables an already-active subscription', async () => {
    registration.pushManager.getSubscription = vi.fn().mockResolvedValue(subscription);
    api.delete.mockResolvedValue({});
    render(<Account />);

    const disableButton = await screen.findByRole('button', { name: 'Disable' });
    await userEvent.click(disableButton);

    await waitFor(() =>
      expect(api.delete).toHaveBeenCalledWith('/api/notifications/push/subscribe', {
        endpoint: subscription.endpoint,
      })
    );
    expect(subscription.unsubscribe).toHaveBeenCalled();
    expect(await screen.findByText(/Get session updates/)).toBeInTheDocument();
  });

  it('reflects a denied browser permission', async () => {
    window.Notification.permission = 'denied';
    render(<Account />);
    expect(await screen.findByText(/blocked/)).toBeInTheDocument();
  });
});

describe('host card', () => {
  it('shows for drivers, linking to the cpo host', async () => {
    render(<Account />);
    const link = await screen.findByRole('link', { name: 'Host your chargers' });
    expect(link).toHaveAttribute('href', 'https://cpo.amphive.app/cpo');
  });

  it('hides for cpo/admin roles', () => {
    mockUser = { ...baseUser, role: 'cpo' };
    render(<Account />);
    expect(screen.queryByRole('link', { name: 'Host your chargers' })).not.toBeInTheDocument();
  });
});
