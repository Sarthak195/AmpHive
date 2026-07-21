/**
 * Groups tests: list loading/error/empty states, join-by-code flow, and
 * leave-group (private-only) via ConfirmDialog.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import Groups from './Groups';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../components/ui', () => ({ useToast: () => toast }));

const GROUPS = [
  { id: 1, name: 'Sunrise Society', is_public: false, plug_count: 3 },
  { id: 2, name: 'Open Mall Lot', is_public: true, plug_count: 7 },
];

const renderGroups = () =>
  render(
    <MemoryRouter>
      <Groups />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(GROUPS);
});

describe('groups list', () => {
  it('renders public/private badges, plug counts and view-chargers links', async () => {
    renderGroups();

    expect(await screen.findByText('Sunrise Society')).toBeInTheDocument();
    expect(screen.getByText('Open Mall Lot')).toBeInTheDocument();
    expect(screen.getByText('Private')).toBeInTheDocument();
    expect(screen.getByText('Public')).toBeInTheDocument();
    expect(screen.getByText('3 chargers')).toBeInTheDocument();
    expect(screen.getByText('7 chargers')).toBeInTheDocument();

    const links = screen.getAllByRole('link', { name: 'View chargers' });
    expect(links[0]).toHaveAttribute('href', '/?group=1');
    expect(links[1]).toHaveAttribute('href', '/?group=2');
  });

  it('shows Leave only for private groups', async () => {
    renderGroups();
    await screen.findByText('Sunrise Society');

    expect(screen.getAllByRole('button', { name: 'Leave' })).toHaveLength(1);
  });

  it('shows a retryable error instead of an empty state on failure', async () => {
    api.get.mockRejectedValueOnce(new Error('down'));
    renderGroups();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.queryByText('No groups yet')).not.toBeInTheDocument();

    api.get.mockResolvedValue(GROUPS);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Sunrise Society')).toBeInTheDocument();
  });

  it('shows the empty state with a map link for a true zero-group result', async () => {
    api.get.mockResolvedValue([]);
    renderGroups();

    expect(await screen.findByText('No groups yet')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Browse the map' })).toHaveAttribute('href', '/map');
  });
});

describe('join a group', () => {
  it('uppercases the code, joins, toasts, clears the field and refetches', async () => {
    api.post.mockResolvedValue({ status: 'joined', group_id: 3, group_name: 'New Group' });
    renderGroups();
    await screen.findByText('Sunrise Society');

    await userEvent.type(screen.getByLabelText('Access code'), 'newcode1');
    await userEvent.click(screen.getByRole('button', { name: 'Join' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/groups/join', { access_code: 'NEWCODE1' })
    );
    expect(toast.ok).toHaveBeenCalledWith('Joined "New Group"');
    expect(screen.getByLabelText('Access code')).toHaveValue('');
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('disables Join until a code is entered', async () => {
    renderGroups();
    await screen.findByText('Sunrise Society');
    expect(screen.getByRole('button', { name: 'Join' })).toBeDisabled();
  });

  it('toasts the friendly error on a bad code without touching the list', async () => {
    api.post.mockRejectedValue(Object.assign(new Error('Invalid access code'), {}));
    renderGroups();
    await screen.findByText('Sunrise Society');

    await userEvent.type(screen.getByLabelText('Access code'), 'BADCODE');
    await userEvent.click(screen.getByRole('button', { name: 'Join' }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith('Invalid access code'));
  });
});

describe('leave a group', () => {
  it('confirms, calls the leave endpoint, toasts and refetches', async () => {
    api.delete.mockResolvedValue({ status: 'left', group_id: 1 });
    renderGroups();
    await screen.findByText('Sunrise Society');

    await userEvent.click(screen.getByRole('button', { name: 'Leave' }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Leave Sunrise Society?');

    await userEvent.click(screen.getByRole('button', { name: 'Leave group' }));

    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/groups/1/leave'));
    expect(toast.ok).toHaveBeenCalledWith('Left "Sunrise Society"');
    expect(api.get).toHaveBeenCalledTimes(2);
  });

  it('toasts the error and keeps the group listed when leave fails', async () => {
    api.delete.mockRejectedValue(new Error('You are not a member of this group.'));
    renderGroups();
    await screen.findByText('Sunrise Society');

    await userEvent.click(screen.getByRole('button', { name: 'Leave' }));
    await userEvent.click(screen.getByRole('button', { name: 'Leave group' }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith('You are not a member of this group.')
    );
    expect(screen.getByText('Sunrise Society')).toBeInTheDocument();
  });

  it('closing the dialog does not call the leave endpoint', async () => {
    renderGroups();
    await screen.findByText('Sunrise Society');

    await userEvent.click(screen.getByRole('button', { name: 'Leave' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(api.delete).not.toHaveBeenCalled();
  });
});
