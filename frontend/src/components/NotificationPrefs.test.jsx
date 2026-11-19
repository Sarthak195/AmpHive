/**
 * NotificationPrefs tests: loads the saved toggles, saves the edited prefs
 * with quiet hours, blocks an empty quiet-hours window, and a read-only
 * cpo_viewer sees nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import NotificationPrefs from './NotificationPrefs';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('./ui', () => ({ useToast: () => toast }));

let mockUser;
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));

const SAVED = {
  push: { session_started: true, nearly_done: false, power_dropped: true },
  quiet_hours: { enabled: false, start: '22:00', end: '07:00' },
  timezone: 'Asia/Kolkata',
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { role: 'driver' };
  api.get.mockResolvedValue(SAVED);
  api.put.mockImplementation(async (_url, body) => ({ ...body, timezone: 'Asia/Kolkata' }));
});

describe('NotificationPrefs', () => {
  it('shows the saved toggles', async () => {
    render(<NotificationPrefs />);
    expect(await screen.findByLabelText('Charging started')).toBeChecked();
    expect(screen.getByLabelText(/Almost done/)).not.toBeChecked();
    expect(api.get).toHaveBeenCalledWith('/api/me/notification-prefs');
  });

  it('saves edited toggles and quiet hours', async () => {
    const user = userEvent.setup();
    render(<NotificationPrefs />);
    await user.click(await screen.findByLabelText('Charging slowed down'));
    await user.click(screen.getByLabelText(/Quiet hours/));
    expect(screen.getByText('Times are in Asia/Kolkata.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Save preferences' }));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith('/api/me/notification-prefs', {
      push: { session_started: true, nearly_done: false, power_dropped: false },
      quiet_hours: { enabled: true, start: '22:00', end: '07:00' },
    });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('blocks a quiet-hours window that starts and ends at the same time', async () => {
    api.get.mockResolvedValue({
      ...SAVED,
      quiet_hours: { enabled: true, start: '22:00', end: '22:00' },
    });
    render(<NotificationPrefs />);
    expect(await screen.findByRole('alert')).toHaveTextContent('different times');
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled();
  });

  it('renders nothing for a read-only cpo_viewer', () => {
    mockUser = { role: 'cpo_viewer' };
    const { container } = render(<NotificationPrefs />);
    expect(container).toBeEmptyDOMElement();
    expect(api.get).not.toHaveBeenCalled();
  });
});
