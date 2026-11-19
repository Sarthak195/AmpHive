/**
 * ChargeRoutines tests: the list shows days, local time with its zone and the
 * limit; saving needs a charger and a limit and sends wall-clock time as
 * typed with the "stop when full" opt-in; toggling and deleting hit the
 * owner routes; a read-only cpo_viewer sees nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ChargeRoutines from './ChargeRoutines';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('./ui', () => ({ useToast: () => toast }));

let mockUser;
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));

const ROUTINE = {
  id: 4,
  plug_id: 1,
  plug_name: 'Home bay',
  weekdays: 31,
  start_time: '22:30',
  max_kwh: null,
  max_duration_seconds: 7200,
  auto_stop_when_full: true,
  enabled: true,
  consecutive_failures: 0,
  tenant_timezone: 'Asia/Kolkata',
};

const OTHER = {
  ...ROUTINE,
  id: 5,
  weekdays: 1 | 4 | 64,
  start_time: '06:15',
  max_kwh: 8,
  max_duration_seconds: 5400,
  auto_stop_when_full: false,
  consecutive_failures: 2,
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { role: 'driver' };
  api.get.mockImplementation((url) =>
    Promise.resolve(
      url === '/api/me/routines'
        ? { routines: [ROUTINE, OTHER], max_routines: 5 }
        : [{ id: 1, name: 'Home bay', tenant_timezone: 'Asia/Kolkata' }],
    ),
  );
  api.post.mockResolvedValue({});
  api.patch.mockResolvedValue({});
  api.delete.mockResolvedValue({});
});

describe('ChargeRoutines', () => {
  it('lists routines with local time, zone and limit', async () => {
    render(<ChargeRoutines />);
    const list = await screen.findByRole('list', { name: 'Your charge routines' });
    expect(within(list).getByText(/Home bay: Weekdays at 22:30/)).toBeInTheDocument();
    expect(within(list).getAllByText('(Asia/Kolkata)')).toHaveLength(2);
    expect(within(list).getByText(/Stops after 2 h, or when the car is full/)).toBeInTheDocument();
    expect(within(list).getByText(/Home bay: Mon Wed Sun at 06:15/)).toBeInTheDocument();
    expect(within(list).getByText(/Stops after 90 min or 8 kWh · didn't start 2 times in a row/)).toBeInTheDocument();
  });

  it('needs a limit, then saves wall-clock time and the full opt-in', async () => {
    render(<ChargeRoutines />);
    await screen.findByRole('option', { name: 'Home bay' });
    await userEvent.selectOptions(screen.getByLabelText('Charger'), '1');
    const save = screen.getByRole('button', { name: 'Save routine' });
    expect(save).toBeDisabled();
    expect(screen.getByText('A routine needs a time or energy limit.')).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText('Energy limit (kWh)'), '6');
    await userEvent.click(screen.getByLabelText('Stop automatically when the car is full'));
    expect(screen.getByLabelText('Start time (Asia/Kolkata)')).toHaveValue('07:00');
    await userEvent.click(save);

    expect(api.post).toHaveBeenCalledWith('/api/me/routines', {
      plug_id: 1,
      weekdays: 31,
      start_time: '07:00',
      max_duration_seconds: null,
      max_kwh: 6,
      auto_stop_when_full: true,
    });
  });

  it('turns a routine off and deletes one after confirming', async () => {
    render(<ChargeRoutines />);
    await userEvent.click(await screen.findByLabelText(/Routine on Home bay at 22:30 is on/));
    expect(api.patch).toHaveBeenCalledWith('/api/me/routines/4', { enabled: false });

    await userEvent.click(screen.getByRole('button', { name: 'Delete routine on Home bay at 22:30' }));
    await userEvent.click(await screen.findByRole('button', { name: 'Delete routine' }));
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/me/routines/4'));
  });

  it('renders nothing for a read-only cpo_viewer', () => {
    mockUser = { role: 'cpo_viewer' };
    const { container } = render(<ChargeRoutines />);
    expect(container).toBeEmptyDOMElement();
    expect(api.get).not.toHaveBeenCalled();
  });
});
