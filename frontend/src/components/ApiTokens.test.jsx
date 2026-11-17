/**
 * ApiTokens tests: the list shows metadata only, a created token is shown
 * once with a copy button, revoke goes through a confirm step, the create
 * control is disabled at the cap, and a read-only cpo_viewer sees nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ApiTokens from './ApiTokens';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('./ui', () => ({ useToast: () => toast }));

let mockUser;
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));

const writeText = vi.fn();

const HA = {
  id: 1,
  name: 'Home Assistant',
  prefix: 'amph_AbC123',
  scope: 'read',
  created_at: '2026-11-18T10:00:00+00:00',
  last_used_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { role: 'driver' };
  api.get.mockResolvedValue({ tokens: [HA], max_active: 5 });
  writeText.mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
});

describe('ApiTokens', () => {
  it('lists active tokens by name and prefix only', async () => {
    render(<ApiTokens />);
    const list = await screen.findByRole('list', { name: 'Active API tokens' });
    expect(within(list).getByText('Home Assistant')).toBeInTheDocument();
    expect(within(list).getByText('amph_AbC123…')).toBeInTheDocument();
    expect(within(list).getByText(/never used/)).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/me/tokens');
  });

  it('creates a token, shows it once and copies it', async () => {
    api.post.mockResolvedValue({
      id: 2, name: 'evcc', token: 'amph_secret-value', prefix: 'amph_secret', created_at: '2026-11-18T11:00:00+00:00',
    });
    render(<ApiTokens />);
    await userEvent.type(screen.getByLabelText('Token name'), '  evcc ');
    await userEvent.click(screen.getByRole('button', { name: 'Create token' }));

    expect(api.post).toHaveBeenCalledWith('/api/me/tokens', { name: 'evcc' });
    expect(await screen.findByTestId('api-token-secret')).toHaveTextContent('amph_secret-value');

    await userEvent.click(screen.getByRole('button', { name: 'Copy token' }));
    expect(writeText).toHaveBeenCalledWith('amph_secret-value');

    await userEvent.click(screen.getByRole('button', { name: 'Done' }));
    expect(screen.queryByTestId('api-token-secret')).not.toBeInTheDocument();
  });

  it('revokes only after confirming', async () => {
    api.delete.mockResolvedValue(null);
    render(<ApiTokens />);
    await userEvent.click(await screen.findByRole('button', { name: 'Revoke Home Assistant' }));
    expect(api.delete).not.toHaveBeenCalled();

    const dialog = screen.getByRole('dialog');
    await userEvent.click(within(dialog).getByRole('button', { name: 'Revoke token' }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/me/tokens/1'));
    expect(toast.ok).toHaveBeenCalledWith('Token revoked.');
  });

  it('disables create at the cap', async () => {
    api.get.mockResolvedValue({ tokens: [HA], max_active: 1 });
    render(<ApiTokens />);
    expect(await screen.findByText('Revoke a token to create another.')).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText('Token name'), 'more');
    expect(screen.getByRole('button', { name: 'Create token' })).toBeDisabled();
  });

  it('renders nothing for a read-only cpo_viewer', () => {
    mockUser = { role: 'cpo_viewer' };
    const { container } = render(<ApiTokens />);
    expect(container).toBeEmptyDOMElement();
    expect(api.get).not.toHaveBeenCalled();
  });
});
