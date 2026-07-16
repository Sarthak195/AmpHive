/**
 * DisputeModal tests: the modal renders its heading + reason field, Submit is
 * blocked until the trimmed reason clears the backend's 10-char minimum, a
 * valid submit POSTs {reason} to /api/sessions/{id}/dispute and then calls
 * onSubmitted (with the created dispute) + onClose, and a server rejection
 * (409 "open dispute already exists") renders inline without closing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import DisputeModal from './DisputeModal';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { post: vi.fn(), get: vi.fn() },
}));

beforeEach(() => {
  vi.clearAllMocks();
  api.post.mockResolvedValue({});
});

describe('DisputeModal', () => {
  it('renders the heading and the reason textarea', () => {
    render(<DisputeModal sessionId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Report an issue' })).toBeInTheDocument();
    expect(screen.getByLabelText('What went wrong?')).toBeInTheDocument();
  });

  it('keeps Submit disabled until the reason is at least 10 chars, then POSTs it', async () => {
    const onClose = vi.fn();
    const onSubmitted = vi.fn();
    const created = { id: 99, session_id: 42, status: 'open' };
    api.post.mockResolvedValue(created);

    render(<DisputeModal sessionId={42} onClose={onClose} onSubmitted={onSubmitted} />);

    const submit = screen.getByRole('button', { name: 'Submit' });
    const textarea = screen.getByLabelText('What went wrong?');

    // Empty and too-short reasons keep Submit disabled.
    expect(submit).toBeDisabled();
    await userEvent.type(textarea, 'bad');
    expect(submit).toBeDisabled();

    // A reason past the 10-char minimum enables and sends it (trimmed).
    await userEvent.clear(textarea);
    const reason = 'The charger stopped early but I was billed for the full session.';
    await userEvent.type(textarea, reason);
    expect(submit).toBeEnabled();

    await userEvent.click(submit);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/sessions/42/dispute', { reason })
    );
    expect(onSubmitted).toHaveBeenCalledWith(created);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('renders a 409 (open dispute already exists) inline and keeps the modal open', async () => {
    const onClose = vi.fn();
    api.post.mockRejectedValue(new Error('An open dispute already exists for this session.'));

    render(<DisputeModal sessionId={42} onClose={onClose} onSubmitted={vi.fn()} />);

    await userEvent.type(
      screen.getByLabelText('What went wrong?'),
      'The charger stopped early but I was billed for the full session.'
    );
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));

    expect(
      await screen.findByText(/An open dispute already exists/)
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    // Still open and usable for another attempt.
    expect(screen.getByRole('button', { name: 'Submit' })).toBeEnabled();
  });
});
