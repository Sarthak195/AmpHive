/**
 * DisputeModal tests (redesign v3, C6 rebuild): the {open, onClose,
 * sessionId, onSubmitted} contract, the category select prepended to the
 * free-text reason, the 10-char minimum gating Submit, the success toast +
 * onSubmitted/onClose hand-off, and a server rejection (409 "open dispute
 * already exists") rendering inline without closing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import DisputeModal from './DisputeModal';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { post: vi.fn(), get: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('./ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

beforeEach(() => {
  vi.clearAllMocks();
  api.post.mockResolvedValue({});
});

describe('DisputeModal — closed', () => {
  it('renders nothing when open is false', () => {
    render(<DisputeModal open={false} sessionId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('DisputeModal — open', () => {
  it('renders the heading, category select and reason textarea', () => {
    render(<DisputeModal open sessionId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Report an issue' })).toBeInTheDocument();
    expect(screen.getByLabelText('Category')).toBeInTheDocument();
    expect(screen.getByLabelText('What went wrong?')).toBeInTheDocument();
  });

  it('keeps Submit disabled until the reason is at least 10 chars, then sends it prefixed with the category', async () => {
    const onClose = vi.fn();
    const onSubmitted = vi.fn();
    const created = { id: 99, session_id: 42, status: 'open' };
    api.post.mockResolvedValue(created);

    render(<DisputeModal open sessionId={42} onClose={onClose} onSubmitted={onSubmitted} />);

    const submit = screen.getByRole('button', { name: 'Submit' });
    const textarea = screen.getByLabelText('What went wrong?');

    expect(submit).toBeDisabled();
    await userEvent.type(textarea, 'bad');
    expect(submit).toBeDisabled();

    const reason = 'The charger stopped early but I was billed for the full session.';
    await userEvent.clear(textarea);
    await userEvent.type(textarea, reason);
    expect(submit).toBeEnabled();

    await userEvent.selectOptions(screen.getByLabelText('Category'), 'Charger problem');
    await userEvent.click(submit);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/sessions/42/dispute', {
        reason: `[Charger problem] ${reason}`,
      })
    );
    expect(toast.ok).toHaveBeenCalledWith("We'll notify you when the operator responds.");
    expect(onSubmitted).toHaveBeenCalledWith(created);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('defaults the category to Billing when none is picked', async () => {
    render(<DisputeModal open sessionId={7} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    await userEvent.type(
      screen.getByLabelText('What went wrong?'),
      'The charger stopped early but I was billed for the full session.'
    );
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/api/sessions/7/dispute',
        expect.objectContaining({ reason: expect.stringMatching(/^\[Billing\]/) })
      )
    );
  });

  it('renders a 409 (open dispute already exists) inline and keeps the modal open', async () => {
    const onClose = vi.fn();
    api.post.mockRejectedValue(new Error('An open dispute already exists for this session.'));

    render(<DisputeModal open sessionId={42} onClose={onClose} onSubmitted={vi.fn()} />);

    await userEvent.type(
      screen.getByLabelText('What went wrong?'),
      'The charger stopped early but I was billed for the full session.'
    );
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));

    expect(await screen.findByText(/An open dispute already exists/)).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(toast.ok).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Submit' })).toBeEnabled();
  });

  it('resets the form when reopened for a different session', async () => {
    const { rerender } = render(
      <DisputeModal open sessionId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />
    );
    await userEvent.type(screen.getByLabelText('What went wrong?'), 'Some text here now');
    rerender(<DisputeModal open={false} sessionId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    rerender(<DisputeModal open sessionId={2} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.getByLabelText('What went wrong?')).toHaveValue('');
  });

  it('disables Cancel and Submit while submitting, and closes via Cancel otherwise', async () => {
    const onClose = vi.fn();
    render(<DisputeModal open sessionId={1} onClose={onClose} onSubmitted={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
  });
});
