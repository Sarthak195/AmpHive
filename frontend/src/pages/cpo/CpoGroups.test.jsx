/**
 * CpoGroups tests: master-detail list/error/empty states, selecting a group
 * shows its access code / members / chargers / circuit sections, access-code
 * copy + regenerate (ConfirmDialog), member removal (ConfirmDialog), the
 * circuit limit editor, delete (ConfirmDialog with the concrete consequence),
 * create + edit modals, and a members-endpoint failure degrading to an inline
 * ErrorState instead of taking down the page.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoGroups from './CpoGroups';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', () => ({ useToast: () => toast }));

vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({ user: { email: 'cpo@amphive.test', full_name: 'Ops', role: 'cpo' }, logout: vi.fn() }),
}));

vi.mock('../../contexts/SessionContext', () => ({
  useSession: () => ({ socket: null, alarms: [] }),
}));

Object.defineProperty(navigator, 'clipboard', {
  value: { writeText: vi.fn().mockResolvedValue(undefined) },
  configurable: true,
});

const GROUPS = [
  {
    id: 1, name: 'Sunrise Society', is_public: false, access_code: 'SUNRISE24',
    plug_count: 3, member_count: 2, max_current_a: 32, current_load_a: 28,
    pending_capacity_requests: 1, created_at: '2026-07-01T00:00:00Z',
  },
  {
    id: 2, name: 'Open Mall Lot', is_public: true, access_code: null,
    plug_count: 5, member_count: 0, max_current_a: null, current_load_a: null,
    pending_capacity_requests: 0, created_at: '2026-07-02T00:00:00Z',
  },
];

const PLUGS = [
  { id: 10, name: 'Charger A', group_id: 1 },
  { id: 11, name: 'Charger B', group_id: 1 },
  { id: 12, name: 'Charger C', group_id: 2 },
];

const MEMBERS_1 = [
  { user_id: 100, email: 'driver1@amphive.test', full_name: 'Driver One', joined_at: '2026-07-03T00:00:00Z' },
  { user_id: 101, email: 'driver2@amphive.test', full_name: null, joined_at: '2026-07-04T00:00:00Z' },
];

const mockApi = ({ groups = GROUPS, plugs = PLUGS, membersByGroup = { 1: MEMBERS_1 } } = {}) => {
  api.get.mockImplementation((url) => {
    if (url === '/api/cpo/groups') return Promise.resolve(groups);
    if (url === '/api/cpo/plugs') return Promise.resolve(plugs);
    if (url === '/api/notifications?limit=20') {
      return Promise.resolve({ notifications: [], unread_count: 0 });
    }
    const membersMatch = url.match(/^\/api\/cpo\/groups\/(\d+)\/members$/);
    if (membersMatch) {
      const entry = membersByGroup[membersMatch[1]];
      if (entry === 'error') return Promise.reject(new Error('down'));
      return Promise.resolve(entry || []);
    }
    return Promise.resolve([]);
  });
};

const renderPage = () => render(<MemoryRouter><CpoGroups /></MemoryRouter>);

beforeEach(() => {
  vi.clearAllMocks();
  mockApi();
});

describe('groups list', () => {
  it('renders group cards with badges, counts and a circuit meter', async () => {
    renderPage();

    expect(await screen.findByText('Sunrise Society')).toBeInTheDocument();
    expect(screen.getByText('Open Mall Lot')).toBeInTheDocument();
    expect(screen.getByText('Private')).toBeInTheDocument();
    expect(screen.getByText('Public')).toBeInTheDocument();
    expect(screen.getByText('3 chargers')).toBeInTheDocument();
    expect(screen.getByText('2 members')).toBeInTheDocument();
    expect(screen.getByText('28 / 32 A')).toBeInTheDocument();
    expect(screen.getByText('1 waiting for capacity')).toBeInTheDocument();
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockImplementation((url) => {
      if (url === '/api/cpo/groups') return Promise.reject(new Error('down'));
      return Promise.resolve([]);
    });
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No groups yet')).not.toBeInTheDocument();

    mockApi();
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Sunrise Society')).toBeInTheDocument();
  });

  it('shows the empty state with a create action for zero groups', async () => {
    mockApi({ groups: [] });
    renderPage();

    expect(await screen.findByText('No groups yet')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Create group/ }).length).toBeGreaterThan(0);
  });

  it('prompts to select a group before any card is clicked', async () => {
    renderPage();
    await screen.findByText('Sunrise Society');
    expect(screen.getByText('Select a group')).toBeInTheDocument();
  });
});

describe('group detail', () => {
  it('shows access code, members, chargers and circuit sections for a private group', async () => {
    renderPage();
    await screen.findByText('Sunrise Society');

    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));

    expect(screen.getByText('SUNRISE24')).toBeInTheDocument();
    expect(await screen.findByText('Driver One')).toBeInTheDocument();
    expect(screen.getByText(/driver2@amphive\.test/)).toBeInTheDocument();
    expect(screen.getByText('Charger A')).toBeInTheDocument();
    expect(screen.getByText('Charger B')).toBeInTheDocument();
    expect(screen.queryByText('Charger C')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'View in Chargers' })).toHaveAttribute(
      'href',
      '/cpo/chargers?group=1'
    );
    expect(
      screen.getByText('Below-16A limits are enforced when starting sessions, not by the plug hardware.')
    ).toBeInTheDocument();
  });

  it('hides Access and Members for a public group', async () => {
    renderPage();
    await screen.findByText('Sunrise Society');

    await userEvent.click(screen.getByRole('button', { name: /Open Mall Lot/ }));

    expect(await screen.findByText('Charger C')).toBeInTheDocument();
    expect(screen.queryByText('Access')).not.toBeInTheDocument();
    expect(screen.queryByText('Members')).not.toBeInTheDocument();
  });

  it('degrades to an inline ErrorState when the members endpoint fails', async () => {
    mockApi({ membersByGroup: { 1: 'error' } });
    renderPage();
    await screen.findByText('Sunrise Society');

    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    // The rest of the panel still renders around the failed section.
    expect(screen.getByText('SUNRISE24')).toBeInTheDocument();
  });

  it('copies the access code to the clipboard', async () => {
    renderPage();
    await screen.findByText('Sunrise Society');
    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));

    await userEvent.click(screen.getByRole('button', { name: 'Copy' }));

    expect(navigator.clipboard.writeText).toHaveBeenCalledWith('SUNRISE24');
    expect(toast.ok).toHaveBeenCalledWith('Access code copied.');
  });
});

describe('regenerate access code', () => {
  it('confirms with the concrete consequence, regenerates and refetches', async () => {
    api.put.mockResolvedValue({ status: 'updated' });
    renderPage();
    await screen.findByText('Sunrise Society');
    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));

    await userEvent.click(screen.getByRole('button', { name: 'New code' }));
    expect(screen.getByRole('dialog')).toHaveTextContent("Every member's saved code stops working");

    await userEvent.click(screen.getByRole('button', { name: 'Generate new code' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/groups/1', { regenerate_access_code: true })
    );
    expect(toast.ok).toHaveBeenCalledWith('New access code generated.');
  });
});

describe('circuit limit', () => {
  it('saves the shared capacity', async () => {
    api.put.mockResolvedValue({ status: 'updated' });
    renderPage();
    await screen.findByText('Sunrise Society');
    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));

    const input = screen.getByLabelText('Shared capacity (A)');
    expect(input).toHaveValue(32);
    await userEvent.clear(input);
    await userEvent.type(input, '40');
    await userEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/groups/1', { max_current_a: 40 })
    );
    expect(toast.ok).toHaveBeenCalledWith('Circuit limit updated.');
  });
});

describe('remove member', () => {
  it('confirms, removes and refetches the member list', async () => {
    api.delete.mockResolvedValue({ status: 'removed' });
    renderPage();
    await screen.findByText('Sunrise Society');
    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));
    await screen.findByText('Driver One');

    await userEvent.click(screen.getByRole('button', { name: /Remove Driver One/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Remove member' }));

    await waitFor(() =>
      expect(api.delete).toHaveBeenCalledWith('/api/cpo/groups/1/members/100')
    );
    expect(toast.ok).toHaveBeenCalledWith('Removed driver1@amphive.test.');
  });
});

describe('delete group', () => {
  it('states the concrete consequence, deletes, clears selection and refetches', async () => {
    api.delete.mockResolvedValue({ status: 'deleted' });
    renderPage();
    await screen.findByText('Sunrise Society');
    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));

    await userEvent.click(screen.getByRole('button', { name: 'Delete group' }));
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent(
      '3 chargers will become public and visible to all users. 2 members will lose access.'
    );

    await userEvent.click(within(dialog).getByRole('button', { name: 'Delete group' }));

    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/cpo/groups/1'));
    expect(toast.ok).toHaveBeenCalledWith('Deleted "Sunrise Society".');
    expect(await screen.findByText('Select a group')).toBeInTheDocument();
  });
});

describe('create group', () => {
  it('creates a group, toasts, closes the modal and selects the new group', async () => {
    api.post.mockResolvedValue({ status: 'created', group_id: 3, name: 'New Group', is_public: false });
    renderPage();
    await screen.findByText('Sunrise Society');

    await userEvent.click(screen.getByRole('button', { name: 'Create group' }));
    const dialog = screen.getByRole('dialog');
    await userEvent.type(within(dialog).getByLabelText('Group name'), 'New Group');
    await userEvent.click(within(dialog).getByRole('radio', { name: 'Public — open to every driver' }));
    await userEvent.click(within(dialog).getByRole('button', { name: 'Create group' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/groups', { name: 'New Group', is_public: true })
    );
    expect(toast.ok).toHaveBeenCalledWith('Created "New Group".');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('edit group', () => {
  it('sends only the changed fields', async () => {
    api.put.mockResolvedValue({ status: 'updated' });
    renderPage();
    await screen.findByText('Sunrise Society');
    await userEvent.click(screen.getByRole('button', { name: /Sunrise Society/ }));

    await userEvent.click(screen.getByRole('button', { name: 'Edit' }));
    const nameInput = screen.getByLabelText('Group name');
    await userEvent.clear(nameInput);
    await userEvent.type(nameInput, 'Sunrise Society Renamed');
    await userEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/cpo/groups/1', { name: 'Sunrise Society Renamed' })
    );
  });
});
