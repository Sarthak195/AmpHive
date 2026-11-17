/**
 * CpoAudit tests: renders the tenant audit trail from GET /api/cpo/audit,
 * pages with Load more (a bare array, so a full page means "maybe more"),
 * and distinguishes empty from error.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoAudit from './CpoAudit';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div data-testid="cpo-layout">{children}</div>,
}));

const entry = (id, action = 'gateway.delete') => ({
  id,
  actor_user_id: 1,
  actor_email: 'cpo@amphive.test',
  action,
  target_type: 'gateway',
  target_id: `gw${id}`,
  detail: `detail ${id}`,
  created_at: '2026-11-17T10:00:00Z',
});

beforeEach(() => vi.clearAllMocks());

describe('CpoAudit', () => {
  it('lists audit rows and hides Load more on a short page', async () => {
    api.get.mockResolvedValue([entry(1, 'plug.create'), entry(2)]);
    render(<CpoAudit />);

    expect(await screen.findByText('plug.create')).toBeInTheDocument();
    expect(screen.getAllByText('cpo@amphive.test').length).toBeGreaterThan(0);
    expect(api.get).toHaveBeenCalledWith('/api/cpo/audit?limit=50&offset=0');
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('Load more requests the next offset and appends', async () => {
    const full = Array.from({ length: 50 }, (_, i) => entry(i + 1));
    api.get.mockResolvedValueOnce(full).mockResolvedValueOnce([entry(99, 'group.delete')]);
    const user = userEvent.setup();
    render(<CpoAudit />);

    await user.click(await screen.findByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('group.delete')).toBeInTheDocument();
    expect(api.get).toHaveBeenLastCalledWith('/api/cpo/audit?limit=50&offset=50');
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('drops rows already shown when pages overlap', async () => {
    const full = Array.from({ length: 50 }, (_, i) => entry(i + 1, `act.${i + 1}`));
    // A new audit row shifted the offset window: id 50 comes back again.
    api.get.mockResolvedValueOnce(full).mockResolvedValueOnce([entry(50, 'act.50'), entry(99, 'group.delete')]);
    const user = userEvent.setup();
    render(<CpoAudit />);

    await user.click(await screen.findByRole('button', { name: 'Load more' }));
    await screen.findByText('group.delete');
    expect(screen.getAllByText('act.50')).toHaveLength(1);
  });

  it('shows an empty state, and an error state on failure', async () => {
    api.get.mockResolvedValueOnce([]);
    const { unmount } = render(<CpoAudit />);
    expect(await screen.findByText('No audit entries yet')).toBeInTheDocument();
    unmount();

    api.get.mockRejectedValueOnce(new Error('down'));
    render(<CpoAudit />);
    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
  });
});
