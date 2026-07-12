/**
 * ReserveModal tests: the plug's upcoming windows are fetched and shown (so
 * drivers book around them), a booking POSTs {plug_id, start_at, end_at} as
 * ISO strings derived from the picked date/time + duration preset, and a
 * server rejection (409 slot taken / cap reached) renders inline without
 * closing the modal.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ReserveModal from './ReserveModal';
import api from '../api/client';
import { fmtWindow } from '../utils/reservationTime';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const PLUG = { id: 7, name: 'Lobby Plug' };

const WINDOWS = [
  {
    id: 1,
    start_at: '2030-01-01T10:00:00+00:00',
    end_at: '2030-01-01T11:00:00+00:00',
    status: 'booked',
    user_name: 'Asha',
    is_mine: false,
  },
  {
    id: 2,
    start_at: '2030-01-01T12:00:00+00:00',
    end_at: '2030-01-01T13:00:00+00:00',
    status: 'booked',
    user_name: 'Me',
    is_mine: true,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue([]);
  api.post.mockResolvedValue({});
});

describe('ReserveModal', () => {
  it("fetches and shows the plug's upcoming windows, marking the caller's own", async () => {
    api.get.mockResolvedValue(WINDOWS);
    render(<ReserveModal plug={PLUG} onClose={vi.fn()} onBooked={vi.fn()} />);

    expect(api.get).toHaveBeenCalledWith('/api/plugs/7/reservations');

    // Someone else's window carries the holder's name; the caller's own is
    // marked "(yours)" instead.
    expect(
      await screen.findByText(new RegExp(`${fmtWindow(WINDOWS[0].start_at, WINDOWS[0].end_at)} — Asha`))
    ).toBeInTheDocument();
    expect(screen.getByText(/\(yours\)/)).toBeInTheDocument();
    expect(screen.getAllByRole('listitem')).toHaveLength(2);
  });

  it('shows a clear-schedule note when there are no upcoming windows', async () => {
    render(<ReserveModal plug={PLUG} onClose={vi.fn()} onBooked={vi.fn()} />);
    expect(
      await screen.findByText(/No upcoming reservations — the schedule is clear\./)
    ).toBeInTheDocument();
  });

  it('POSTs {plug_id, start_at, end_at} ISO strings from date + time + duration preset', async () => {
    const onBooked = vi.fn();
    render(<ReserveModal plug={PLUG} onClose={vi.fn()} onBooked={onBooked} />);

    fireEvent.change(screen.getByLabelText('Reservation date'), {
      target: { value: '2030-01-05' },
    });
    fireEvent.change(screen.getByLabelText('Reservation start time'), {
      target: { value: '10:30' },
    });
    await userEvent.click(screen.getByRole('button', { name: '2 h' }));
    await userEvent.click(screen.getByRole('button', { name: 'Reserve' }));

    // The modal builds a LOCAL Date from the inputs and sends its UTC ISO
    // form — recompute the same way so the assertion is timezone-proof.
    const expectedStart = new Date('2030-01-05T10:30');
    const expectedEnd = new Date(expectedStart.getTime() + 120 * 60 * 1000);
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/reservations', {
        plug_id: 7,
        start_at: expectedStart.toISOString(),
        end_at: expectedEnd.toISOString(),
      })
    );
    expect(onBooked).toHaveBeenCalled();
  });

  it('defaults the duration preset to 1 h', async () => {
    render(<ReserveModal plug={PLUG} onClose={vi.fn()} onBooked={vi.fn()} />);
    expect(screen.getByRole('button', { name: '1 h' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: '30 min' })).toHaveAttribute('aria-pressed', 'false');
  });

  it('renders a 409 (slot taken / cap reached) inline and keeps the modal open', async () => {
    const onBooked = vi.fn();
    api.post.mockRejectedValue(
      new Error('That slot overlaps an existing reservation (2030-01-05 10:00–11:00 UTC).')
    );
    render(<ReserveModal plug={PLUG} onClose={vi.fn()} onBooked={onBooked} />);

    fireEvent.change(screen.getByLabelText('Reservation date'), {
      target: { value: '2030-01-05' },
    });
    fireEvent.change(screen.getByLabelText('Reservation start time'), {
      target: { value: '10:30' },
    });
    await userEvent.click(screen.getByRole('button', { name: 'Reserve' }));

    expect(
      await screen.findByText(/That slot overlaps an existing reservation/)
    ).toBeInTheDocument();
    expect(onBooked).not.toHaveBeenCalled();
    // Still open and usable for another attempt.
    expect(screen.getByRole('button', { name: 'Reserve' })).toBeEnabled();
  });
});
