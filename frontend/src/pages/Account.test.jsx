/**
 * Account tests: read-only profile fields (member-since only when the API
 * provides it — no fake data), the password-reset trigger, the web-push
 * enable/disable/unsupported/denied states (moved here from
 * NotificationBell), the driver-only "Host your chargers" card, and the
 * "Your data" card — the self-service export download and the account-closure
 * flow, which is the most dangerous control in the app and therefore the one
 * with the most coverage here: the consequences must be stated before the
 * confirm button can be pressed, the phrase must be typed exactly, password
 * accounts must re-authenticate, Google-only accounts must NOT be asked for a
 * password they don't have, and every server refusal must land in front of
 * the user instead of silently closing the dialog.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import Account from './Account';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../components/ui', () => ({ useToast: () => toast }));

vi.mock('../utils/appHost', () => ({ cpoOrigin: () => 'https://cpo.amphive.app' }));

vi.mock('../contexts/ConfigContext', () => ({ useConfig: () => ({ coin_inr_rate: 1 }) }));

let mockUser;
const logoutSpy = vi.fn();
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ user: mockUser, logout: logoutSpy }),
}));

const baseUser = {
  email: 'driver@amphive.test',
  full_name: 'Driver One',
  role: 'driver',
  auth_provider: 'password',
  coin_balance: 0,
};

// Account links to /privacy, so the page needs a router in scope.
const renderAccount = () =>
  render(
    <MemoryRouter>
      <Account />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { ...baseUser };
  logoutSpy.mockResolvedValue(undefined);
});

describe('profile card', () => {
  it('shows name and email', () => {
    renderAccount();
    expect(screen.getByText('Driver One')).toBeInTheDocument();
    expect(screen.getByText('driver@amphive.test')).toBeInTheDocument();
  });

  it('omits member-since when the API does not provide created_at', () => {
    renderAccount();
    expect(screen.queryByText('Member since')).not.toBeInTheDocument();
  });

  it('shows member-since when created_at is present', () => {
    mockUser = { ...baseUser, created_at: '2026-01-15T00:00:00Z' };
    renderAccount();
    expect(screen.getByText('Member since')).toBeInTheDocument();
    expect(screen.getByText('January 2026')).toBeInTheDocument();
  });
});

describe('security card', () => {
  it('sends the reset email to the signed-in address and confirms', async () => {
    api.post.mockResolvedValue({});
    renderAccount();

    await userEvent.click(screen.getByRole('button', { name: 'Reset your password' }));

    expect(api.post).toHaveBeenCalledWith('/api/auth/forgot-password', {
      email: 'driver@amphive.test',
    });
    expect(await screen.findByText(/Check your inbox/)).toBeInTheDocument();
    expect(toast.ok).toHaveBeenCalled();
  });

  it('surfaces failures via toast instead of a silent no-op', async () => {
    api.post.mockRejectedValue(new Error('Too many requests. Try again later.'));
    renderAccount();

    await userEvent.click(screen.getByRole('button', { name: 'Reset your password' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('Too many requests. Try again later.')
    );
    expect(screen.queryByText(/Check your inbox/)).not.toBeInTheDocument();
  });
});

describe('notifications card — push unsupported (default jsdom environment)', () => {
  it('shows an unsupported message with no toggle button', async () => {
    renderAccount();
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
    renderAccount();
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
    renderAccount();

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
    renderAccount();
    expect(await screen.findByText(/blocked/)).toBeInTheDocument();
  });
});

describe('host card', () => {
  it('shows for drivers, linking to the cpo host', async () => {
    renderAccount();
    const link = await screen.findByRole('link', { name: 'Host your chargers' });
    expect(link).toHaveAttribute('href', 'https://cpo.amphive.app/cpo');
  });

  it('hides for cpo/admin roles', () => {
    mockUser = { ...baseUser, role: 'cpo' };
    renderAccount();
    expect(screen.queryByRole('link', { name: 'Host your chargers' })).not.toBeInTheDocument();
  });
});

describe('your data — export download', () => {
  let clickSpy;

  beforeEach(() => {
    // jsdom implements neither object URLs nor downloads. Stubbing
    // HTMLAnchorElement.click also keeps jsdom from trying (and failing) to
    // navigate to the blob: URL.
    URL.createObjectURL = vi.fn(() => 'blob:amphive/export');
    URL.revokeObjectURL = vi.fn();
    clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  });

  afterEach(() => {
    clickSpy.mockRestore();
  });

  it('fetches the export through the api client and saves it as amphive-data-export.json', async () => {
    api.get.mockResolvedValue({ export_format: 'amphive.user-data-export.v1', account: { id: 7 } });
    renderAccount();

    await userEvent.click(screen.getByRole('button', { name: 'Download your data' }));

    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/auth/me/export'));
    expect(URL.createObjectURL).toHaveBeenCalled();
    // The saved filename is the contract with the user, so assert it on the
    // anchor the click was dispatched on.
    const anchor = clickSpy.mock.instances[0];
    expect(anchor).toHaveAttribute('download', 'amphive-data-export.json');
    expect(anchor).toHaveAttribute('href', 'blob:amphive/export');
    // The blob carries the serialised document, not "[object Object]".
    const [blob] = URL.createObjectURL.mock.calls[0];
    expect(blob.type).toBe('application/json');
    await expect(blob.text()).resolves.toContain('amphive.user-data-export.v1');
    await waitFor(() => expect(toast.ok).toHaveBeenCalled());
  });

  it('explains the hourly cap when the export is rate-limited (429)', async () => {
    const err = new Error('Too many data export attempts on this account. Try again in 42 s.');
    err.status = 429;
    api.get.mockRejectedValue(err);
    renderAccount();

    await userEvent.click(screen.getByRole('button', { name: 'Download your data' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        expect.stringContaining('limited to five an hour')
      )
    );
    // The server's own retry window survives too.
    expect(toast.error).toHaveBeenCalledWith(expect.stringContaining('42 s'));
  });

  it('surfaces any other failure through the toast', async () => {
    api.get.mockRejectedValue(new Error('The request timed out. Check your connection and try again.'));
    renderAccount();

    await userEvent.click(screen.getByRole('button', { name: 'Download your data' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        'The request timed out. Check your connection and try again.'
      )
    );
  });
});

describe('your data — account closure', () => {
  const openDialog = async () => {
    renderAccount();
    await userEvent.click(screen.getByRole('button', { name: 'Close your account' }));
    return screen.getByRole('dialog');
  };

  it('states every consequence before the confirm control', async () => {
    const dialog = await openDialog();

    expect(within(dialog).getByText(/can.t be undone/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/Google sign-in link are erased/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/push notification devices and\s+group memberships are deleted/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/GST invoices are/i)).toBeInTheDocument();
    expect(within(dialog).getByText(/charging credit is forfeited/i)).toBeInTheDocument();
    expect(within(dialog).getByRole('link', { name: 'Privacy Policy' })).toHaveAttribute(
      'href',
      '/privacy'
    );
  });

  it('names the actual balance at risk when the account still holds credit', async () => {
    mockUser = { ...baseUser, coin_balance: 240.5 };
    const dialog = await openDialog();

    expect(within(dialog).getByText('₹240.50')).toBeInTheDocument();
    expect(within(dialog).getByText(/spend it before you close/i)).toBeInTheDocument();
  });

  it('keeps the confirm button disabled until the phrase AND the password are given', async () => {
    const dialog = await openDialog();
    const confirmButton = within(dialog).getByRole('button', {
      name: 'Close my account permanently',
    });
    expect(confirmButton).toBeDisabled();

    // Right phrase, no password → still disabled.
    await userEvent.type(
      within(dialog).getByLabelText('Type DELETE MY ACCOUNT to confirm'),
      'DELETE MY ACCOUNT'
    );
    expect(confirmButton).toBeDisabled();

    await userEvent.type(within(dialog).getByLabelText('Your password'), 'hunter2hunter2');
    expect(confirmButton).toBeEnabled();
  });

  it('stays disabled for a near-miss phrase', async () => {
    const dialog = await openDialog();
    await userEvent.type(
      within(dialog).getByLabelText('Type DELETE MY ACCOUNT to confirm'),
      'delete my account'
    );
    await userEvent.type(within(dialog).getByLabelText('Your password'), 'hunter2hunter2');
    expect(
      within(dialog).getByRole('button', { name: 'Close my account permanently' })
    ).toBeDisabled();
  });

  it('closes the account, clears the session through logout() and does not hand-roll it', async () => {
    api.delete.mockResolvedValue({
      status: 'closed',
      forfeited_coins: 0,
      detail: 'Your account is closed…',
    });
    const dialog = await openDialog();

    await userEvent.type(
      within(dialog).getByLabelText('Type DELETE MY ACCOUNT to confirm'),
      'DELETE MY ACCOUNT'
    );
    await userEvent.type(within(dialog).getByLabelText('Your password'), 'hunter2hunter2');
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Close my account permanently' })
    );

    await waitFor(() =>
      expect(api.delete).toHaveBeenCalledWith('/api/auth/me', {
        confirm: 'DELETE MY ACCOUNT',
        password: 'hunter2hunter2',
      })
    );
    // logout() is the single session-teardown path (context state + both
    // localStorage keys); the page must not re-implement it.
    await waitFor(() => expect(logoutSpy).toHaveBeenCalled());
  });

  it('never asks a Google-only account for a password it does not have', async () => {
    mockUser = { ...baseUser, auth_provider: 'google' };
    api.delete.mockResolvedValue({ status: 'closed', forfeited_coins: 0, detail: 'ok' });
    const dialog = await openDialog();

    expect(within(dialog).queryByLabelText('Your password')).not.toBeInTheDocument();

    await userEvent.type(
      within(dialog).getByLabelText('Type DELETE MY ACCOUNT to confirm'),
      'DELETE MY ACCOUNT'
    );
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Close my account permanently' })
    );

    await waitFor(() =>
      expect(api.delete).toHaveBeenCalledWith('/api/auth/me', { confirm: 'DELETE MY ACCOUNT' })
    );
  });

  it('keeps the dialog open and shows the refusal when the server says no (409)', async () => {
    const err = new Error('You have a charging session in progress. Stop it first, then close your account.');
    err.status = 409;
    api.delete.mockRejectedValue(err);
    const dialog = await openDialog();

    await userEvent.type(
      within(dialog).getByLabelText('Type DELETE MY ACCOUNT to confirm'),
      'DELETE MY ACCOUNT'
    );
    await userEvent.type(within(dialog).getByLabelText('Your password'), 'hunter2hunter2');
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Close my account permanently' })
    );

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/charging session in progress/i);
    // Still open, still recoverable, and the session was NOT torn down.
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(logoutSpy).not.toHaveBeenCalled();
  });

  it('shows the wrong-password refusal (403) in the dialog', async () => {
    const err = new Error('Password is incorrect.');
    err.status = 403;
    api.delete.mockRejectedValue(err);
    const dialog = await openDialog();

    await userEvent.type(
      within(dialog).getByLabelText('Type DELETE MY ACCOUNT to confirm'),
      'DELETE MY ACCOUNT'
    );
    await userEvent.type(within(dialog).getByLabelText('Your password'), 'wrong-password');
    await userEvent.click(
      within(dialog).getByRole('button', { name: 'Close my account permanently' })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent('Password is incorrect.');
  });

  it('can be cancelled without calling the API', async () => {
    const dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(api.delete).not.toHaveBeenCalled();
  });
});
