/**
 * LogoutEverywhere tests: the global revoke sits behind a confirm step,
 * Cancel calls nothing, Confirm calls logoutEverywhere() once and then
 * replaces the page with /login, and a server failure keeps the dialog open
 * with the error (the other devices are still signed in, so the page must
 * not pretend otherwise).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import LogoutEverywhere from './LogoutEverywhere';

const logoutEverywhereSpy = vi.fn();
vi.mock('../contexts/AuthContext', () => ({
  useAuth: () => ({ logoutEverywhere: logoutEverywhereSpy }),
}));

const replaceSpy = vi.fn();
const realLocation = window.location;

beforeEach(() => {
  vi.clearAllMocks();
  logoutEverywhereSpy.mockResolvedValue(undefined);
  Object.defineProperty(window, 'location', {
    configurable: true,
    value: { ...realLocation, replace: replaceSpy },
  });
});

afterEach(() => {
  Object.defineProperty(window, 'location', { configurable: true, value: realLocation });
});

const openDialog = async () => {
  render(<LogoutEverywhere />);
  await userEvent.click(screen.getByRole('button', { name: 'Log out everywhere' }));
  return screen.getByRole('dialog');
};

describe('LogoutEverywhere', () => {
  it('explains that the normal log out is this device only', () => {
    render(<LogoutEverywhere />);
    expect(screen.getByText(/this app in this browser only/i)).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('asks for confirmation before signing out every device', async () => {
    const dialog = await openDialog();
    expect(within(dialog).getByText(/every device and browser, including this/i)).toBeInTheDocument();
    expect(logoutEverywhereSpy).not.toHaveBeenCalled();
  });

  it('can be cancelled without calling the server', async () => {
    const dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(logoutEverywhereSpy).not.toHaveBeenCalled();
  });

  it('signs out everywhere once and replaces the page with /login', async () => {
    const dialog = await openDialog();
    await userEvent.click(within(dialog).getByRole('button', { name: 'Log out everywhere' }));

    await waitFor(() => expect(replaceSpy).toHaveBeenCalledWith('/login'));
    expect(logoutEverywhereSpy).toHaveBeenCalledTimes(1);
  });

  it('keeps the dialog open and shows the error when the server call fails', async () => {
    const err = new Error('Something went wrong on our side.');
    err.status = 500;
    logoutEverywhereSpy.mockRejectedValue(err);
    const dialog = await openDialog();

    await userEvent.click(within(dialog).getByRole('button', { name: 'Log out everywhere' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Something went wrong on our side.');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(replaceSpy).not.toHaveBeenCalled();
    // Busy cleared: the user can retry.
    expect(within(dialog).getByRole('button', { name: 'Log out everywhere' })).toBeEnabled();
  });
});
