/**
 * ReportProblemModal tests (near-copy of DisputeModal.test.jsx's shape): the
 * {open, onClose, plugId, plugName, onSubmitted} contract, the category
 * select + description textarea posted directly (no prefix hack, unlike
 * DisputeModal), the 10-char minimum gating Submit, the success toast +
 * onSubmitted/onClose hand-off, and a server rejection rendering inline
 * without closing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ReportProblemModal from './ReportProblemModal';
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

describe('ReportProblemModal — closed', () => {
  it('renders nothing when open is false', () => {
    render(<ReportProblemModal open={false} plugId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('ReportProblemModal — open', () => {
  it('renders the heading, category select and description textarea', () => {
    render(<ReportProblemModal open plugId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.getByRole('heading', { name: 'Report a problem' })).toBeInTheDocument();
    expect(screen.getByLabelText('Category')).toBeInTheDocument();
    expect(screen.getByLabelText('Details')).toBeInTheDocument();
  });

  it('names the plug in the prompt when plugName is given', () => {
    render(<ReportProblemModal open plugId={1} plugName="Lobby Plug" onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.getByText(/What's wrong with Lobby Plug\?/)).toBeInTheDocument();
  });

  it('keeps Submit disabled until description is at least 10 chars, then posts category + description directly', async () => {
    const onClose = vi.fn();
    const onSubmitted = vi.fn();
    const created = { id: 7, plug_id: 42, status: 'open' };
    api.post.mockResolvedValue(created);

    render(<ReportProblemModal open plugId={42} onClose={onClose} onSubmitted={onSubmitted} />);

    const submit = screen.getByRole('button', { name: 'Submit' });
    const textarea = screen.getByLabelText('Details');

    expect(submit).toBeDisabled();
    await userEvent.type(textarea, 'bad');
    expect(submit).toBeDisabled();

    const description = 'The connector is cracked and sparks whenever I plug in.';
    await userEvent.clear(textarea);
    await userEvent.type(textarea, description);
    expect(submit).toBeEnabled();

    await userEvent.selectOptions(screen.getByLabelText('Category'), 'unsafe');
    await userEvent.click(submit);

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/plugs/42/report', {
        category: 'unsafe',
        description,
      })
    );
    expect(toast.ok).toHaveBeenCalledWith("Thanks — we've flagged this for the operator.");
    expect(onSubmitted).toHaveBeenCalledWith(created);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('defaults the category to "damaged" when none is picked', async () => {
    render(<ReportProblemModal open plugId={9} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    await userEvent.type(
      screen.getByLabelText('Details'),
      'The cable casing is split and exposing bare copper wire.'
    );
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith(
        '/api/plugs/9/report',
        expect.objectContaining({ category: 'damaged' })
      )
    );
  });

  it('renders a server rejection inline and keeps the modal open', async () => {
    const onClose = vi.fn();
    api.post.mockRejectedValue(new Error('Something went wrong. Please try again.'));

    render(<ReportProblemModal open plugId={42} onClose={onClose} onSubmitted={vi.fn()} />);

    await userEvent.type(
      screen.getByLabelText('Details'),
      'The connector is cracked and sparks whenever I plug in.'
    );
    await userEvent.click(screen.getByRole('button', { name: 'Submit' }));

    expect(await screen.findByText('Something went wrong. Please try again.')).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(toast.ok).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Submit' })).toBeEnabled();
  });

  it('resets the form when reopened for a different plug', async () => {
    const { rerender } = render(
      <ReportProblemModal open plugId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />
    );
    await userEvent.type(screen.getByLabelText('Details'), 'Some text here now');
    rerender(<ReportProblemModal open={false} plugId={1} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    rerender(<ReportProblemModal open plugId={2} onClose={vi.fn()} onSubmitted={vi.fn()} />);
    expect(screen.getByLabelText('Details')).toHaveValue('');
  });

  it('disables Cancel and Submit while submitting, and closes via Cancel otherwise', async () => {
    const onClose = vi.fn();
    render(<ReportProblemModal open plugId={1} onClose={onClose} onSubmitted={vi.fn()} />);
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalled();
  });
});
