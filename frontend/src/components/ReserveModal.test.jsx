/**
 * ReserveModal tests — booking a slot around the plug's schedule:
 * the upcoming windows render (with an ErrorState + retry INSIDE the modal
 * when the fetch fails, never a clean-looking empty schedule), the
 * client-side overlap check blocks a clashing window before any POST, a
 * valid submit POSTs {plug_id, start_at, end_at} and calls onReserved, and
 * server rejections surface inline.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ReserveModal from './ReserveModal';
import api from '../api/client';
import { fmtWindow } from '../utils/reservationTime';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

const PLUG = { id: 3, name: 'Rooftop Plug' };

// A busy window on a fixed future day (local time), so overlap checks are
// deterministic regardless of when the test runs.
const BUSY_START = new Date('2030-06-01T10:30:00');
const BUSY_END = new Date('2030-06-01T11:30:00');
const WINDOWS = [
  { id: 9, start_at: BUSY_START.toISOString(), end_at: BUSY_END.toISOString(), is_mine: false, user_name: 'Asha' },
];

const renderModal = (props = {}) => {
  const onReserved = props.onReserved || vi.fn();
  render(<ReserveModal open onClose={props.onClose || vi.fn()} plug={PLUG} onReserved={onReserved} />);
  return { onReserved };
};

const pickWhen = (date, time) => {
  fireEvent.change(screen.getByLabelText('Date'), { target: { value: date } });
  fireEvent.change(screen.getByLabelText('Start time'), { target: { value: time } });
};

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(WINDOWS);
});

describe('ReserveModal — schedule', () => {
  it('fetches and lists the plug’s upcoming windows', async () => {
    renderModal();
    expect(api.get).toHaveBeenCalledWith('/api/plugs/3/reservations');
    expect(await screen.findByText(new RegExp('Asha'))).toBeInTheDocument();
    expect(screen.getByText(new RegExp(fmtWindow(WINDOWS[0].start_at, WINDOWS[0].end_at).slice(0, 6)))).toBeInTheDocument();
  });

  it('a schedule fetch failure shows an ErrorState with retry inside the modal', async () => {
    api.get.mockRejectedValueOnce(new Error('boom'));
    renderModal();

    expect(await screen.findByText("Couldn't load the schedule")).toBeInTheDocument();
    expect(screen.queryByText(/schedule is clear/)).not.toBeInTheDocument();

    api.get.mockResolvedValueOnce([]);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText(/schedule is clear/)).toBeInTheDocument();
  });
});

describe('ReserveModal — overlap check', () => {
  it('blocks a window that clashes with an existing reservation, without POSTing', async () => {
    renderModal();
    await screen.findByText(new RegExp('Asha'));

    pickWhen('2030-06-01', '10:00'); // 10:00–11:00 vs busy 10:30–11:30
    expect(
      await screen.findByText(/overlaps an existing reservation/)
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reserve' })).toBeDisabled();
    expect(api.post).not.toHaveBeenCalled();
  });

  it('a custom end time before the start is rejected client-side', async () => {
    renderModal();
    await screen.findByText(new RegExp('Asha'));

    pickWhen('2030-06-01', '14:00');
    await userEvent.click(screen.getByRole('button', { name: 'Custom end' }));
    fireEvent.change(screen.getByLabelText('End time'), { target: { value: '13:00' } });
    expect(await screen.findByText(/End time must be after the start/)).toBeInTheDocument();
  });
});

describe('ReserveModal — submit', () => {
  it('POSTs {plug_id, start_at, end_at} and calls onReserved on success', async () => {
    api.post.mockResolvedValue({ id: 1 });
    const { onReserved } = renderModal();
    await screen.findByText(new RegExp('Asha'));

    pickWhen('2030-06-01', '14:00'); // default duration 1h → 14:00–15:00
    await userEvent.click(screen.getByRole('button', { name: 'Reserve' }));

    await waitFor(() => expect(onReserved).toHaveBeenCalled());
    expect(api.post).toHaveBeenCalledWith('/api/reservations', {
      plug_id: 3,
      start_at: new Date('2030-06-01T14:00:00').toISOString(),
      end_at: new Date('2030-06-01T15:00:00').toISOString(),
    });
  });

  it('a server rejection surfaces inline and the modal stays open', async () => {
    api.post.mockRejectedValue(new Error('You already have a reservation that overlaps this window.'));
    const { onReserved } = renderModal();
    await screen.findByText(new RegExp('Asha'));

    pickWhen('2030-06-01', '14:00');
    await userEvent.click(screen.getByRole('button', { name: 'Reserve' }));

    expect(
      await screen.findByText(/already have a reservation that overlaps/)
    ).toBeInTheDocument();
    expect(onReserved).not.toHaveBeenCalled();
  });
});
