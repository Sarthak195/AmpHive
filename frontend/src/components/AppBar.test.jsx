/**
 * AppBar tests: anonymous vs signed-in rendering on the driver host (nav
 * links, wallet pill, user menu with the role-aware become-a-host / console
 * link), the CPO-host variant (brand + auth only, no driver navigation), and
 * the menu's Escape/outside-click focus restore to the trigger button.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import AppBar from './AppBar';
import { useAuth } from '../contexts/AuthContext';
import { isCpoHost, isSplitHost, cpoOrigin } from '../utils/appHost';

vi.mock('../contexts/AuthContext', () => ({ useAuth: vi.fn() }));
vi.mock('../contexts/WalletContext', () => ({ useWallet: () => ({ balance: 250 }) }));
vi.mock('./NotificationBell', () => ({ default: () => <div data-testid="bell" /> }));
vi.mock('./ui/Money', () => ({
  default: ({ coins }) => <span data-testid="money">₹{coins}</span>,
}));
vi.mock('../utils/appHost', () => ({
  isCpoHost: vi.fn(),
  isSplitHost: vi.fn(),
  cpoOrigin: vi.fn(),
}));

const renderAppBar = () =>
  render(
    <MemoryRouter>
      <AppBar />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  isCpoHost.mockReturnValue(false);
  isSplitHost.mockReturnValue(true);
  cpoOrigin.mockReturnValue('https://cpo.amphive.app');
});

describe('AppBar on the driver host — anonymous', () => {
  beforeEach(() => useAuth.mockReturnValue({ user: null, logout: vi.fn() }));

  it('shows brand, nav links and a Sign in button; no wallet pill, bell or menu', () => {
    renderAppBar();
    expect(screen.getByRole('link', { name: /amphive/i })).toHaveAttribute('href', '/');
    for (const label of ['Home', 'Map', 'Activity', 'Groups']) {
      expect(screen.getByRole('link', { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole('link', { name: /sign in/i })).toHaveAttribute('href', '/login');
    expect(screen.queryByLabelText(/charging credit balance/i)).not.toBeInTheDocument();
    expect(screen.queryByTestId('bell')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /account menu/i })).not.toBeInTheDocument();
  });
});

describe('AppBar on the driver host — signed in', () => {
  const logout = vi.fn().mockResolvedValue();

  beforeEach(() =>
    useAuth.mockReturnValue({
      user: { email: 'driver@amphive.test', full_name: 'Dana Driver', role: 'driver' },
      logout,
    })
  );

  it('shows the credit pill (linking to /credit), notification bell and avatar initial', () => {
    renderAppBar();
    const pill = screen.getByLabelText(/charging credit balance/i);
    expect(pill).toHaveAttribute('href', '/credit');
    expect(screen.getByTestId('money')).toHaveTextContent('₹250');
    expect(screen.getByTestId('bell')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /account menu/i })).toHaveTextContent('D');
    expect(screen.queryByRole('link', { name: /sign in/i })).not.toBeInTheDocument();
  });

  it('opens the user menu with Account, the external host-your-chargers link, and Sign out', async () => {
    renderAppBar();
    await userEvent.click(screen.getByRole('button', { name: /account menu/i }));

    expect(screen.getByRole('menuitem', { name: /account/i })).toHaveAttribute('href', '/account');
    expect(screen.getByRole('menuitem', { name: /host your chargers/i })).toHaveAttribute(
      'href',
      'https://cpo.amphive.app/cpo'
    );

    await userEvent.click(screen.getByRole('menuitem', { name: /sign out/i }));
    expect(logout).toHaveBeenCalled();
  });

  it('restores focus to the account-menu button on Escape', async () => {
    renderAppBar();
    const trigger = screen.getByRole('button', { name: /account menu/i });
    await userEvent.click(trigger);
    expect(screen.getByRole('menu', { name: 'Account' })).toBeInTheDocument();

    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('menu', { name: 'Account' })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('restores focus to the account-menu button on outside click', async () => {
    renderAppBar();
    const trigger = screen.getByRole('button', { name: /account menu/i });
    await userEvent.click(trigger);
    expect(screen.getByRole('menu', { name: 'Account' })).toBeInTheDocument();

    await userEvent.click(document.body);
    expect(screen.queryByRole('menu', { name: 'Account' })).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });
});

describe('AppBar user menu — cpo role', () => {
  beforeEach(() =>
    useAuth.mockReturnValue({
      user: { email: 'op@amphive.test', full_name: 'Olive Operator', role: 'cpo' },
      logout: vi.fn(),
    })
  );

  it('shows an external "Host console" link on a split (driver) host', async () => {
    isSplitHost.mockReturnValue(true);
    renderAppBar();
    await userEvent.click(screen.getByRole('button', { name: /account menu/i }));

    expect(screen.queryByRole('menuitem', { name: /host your chargers/i })).not.toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /host console/i })).toHaveAttribute(
      'href',
      'https://cpo.amphive.app/cpo/dashboard'
    );
  });

  it('shows an internal "Host console" link on an unsplit host', async () => {
    isSplitHost.mockReturnValue(false);
    renderAppBar();
    await userEvent.click(screen.getByRole('button', { name: /account menu/i }));

    expect(screen.getByRole('menuitem', { name: /host console/i })).toHaveAttribute(
      'href',
      '/cpo/dashboard'
    );
  });
});

describe('AppBar user menu — admin role', () => {
  beforeEach(() =>
    useAuth.mockReturnValue({
      user: { email: 'admin@amphive.test', full_name: 'Ada Admin', role: 'admin' },
      logout: vi.fn(),
    })
  );

  it('shows an external "Admin console" link on a split (driver) host', async () => {
    isSplitHost.mockReturnValue(true);
    renderAppBar();
    await userEvent.click(screen.getByRole('button', { name: /account menu/i }));

    expect(screen.queryByRole('menuitem', { name: /host your chargers/i })).not.toBeInTheDocument();
    expect(screen.getByRole('menuitem', { name: /admin console/i })).toHaveAttribute(
      'href',
      'https://cpo.amphive.app/admin'
    );
  });

  it('shows an internal "Admin console" link on an unsplit host', async () => {
    isSplitHost.mockReturnValue(false);
    renderAppBar();
    await userEvent.click(screen.getByRole('button', { name: /account menu/i }));

    expect(screen.getByRole('menuitem', { name: /admin console/i })).toHaveAttribute(
      'href',
      '/admin'
    );
  });
});

describe('AppBar on the CPO host', () => {
  beforeEach(() => {
    isCpoHost.mockReturnValue(true);
    useAuth.mockReturnValue({
      user: { email: 'op@amphive.test', full_name: 'Olive Operator', role: 'cpo' },
      logout: vi.fn(),
    });
  });

  it('hides driver navigation, wallet pill, and the host-your-chargers menu item', async () => {
    renderAppBar();
    for (const label of ['Home', 'Map', 'Activity', 'Groups']) {
      expect(screen.queryByRole('link', { name: label })).not.toBeInTheDocument();
    }
    expect(screen.queryByLabelText(/charging credit balance/i)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /account menu/i }));
    expect(screen.getByRole('menuitem', { name: /account/i })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: /host your chargers/i })).not.toBeInTheDocument();
  });
});
