/**
 * ConfirmDialog tests: renders title/body inside a Modal, Confirm fires
 * onConfirm, tone maps to .btn-danger-solid / .btn-primary, and busy
 * disables both buttons + swaps in the working label.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import ConfirmDialog from './ConfirmDialog';

describe('ConfirmDialog', () => {
  it('renders the consequence copy and confirms', () => {
    const onConfirm = vi.fn();
    const onClose = vi.fn();
    render(
      <ConfirmDialog
        open
        onClose={onClose}
        onConfirm={onConfirm}
        title="Stop charging?"
        body="This ends the session and bills your wallet."
        confirmLabel="Stop charging"
      />
    );

    expect(screen.getByRole('dialog', { name: 'Stop charging?' })).toBeInTheDocument();
    expect(screen.getByText('This ends the session and bills your wallet.')).toBeInTheDocument();

    const confirm = screen.getByRole('button', { name: 'Stop charging' });
    // Default tone is danger → solid danger button.
    expect(confirm).toHaveClass('btn-danger-solid');
    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('tone="primary" uses the primary button', () => {
    render(
      <ConfirmDialog
        open
        onClose={vi.fn()}
        onConfirm={vi.fn()}
        title="Publish tariff?"
        body="Drivers see the new price immediately."
        confirmLabel="Publish"
        tone="primary"
      />
    );
    expect(screen.getByRole('button', { name: 'Publish' })).toHaveClass('btn-primary');
  });

  it('busy disables both buttons and shows the working label', () => {
    const onClose = vi.fn();
    render(
      <ConfirmDialog
        open
        onClose={onClose}
        onConfirm={vi.fn()}
        title="Delete plug?"
        body="This cannot be undone."
        confirmLabel="Delete"
        busy
      />
    );

    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    const working = screen.getByRole('button', { name: 'Working…' });
    expect(working).toBeDisabled();
    const cancel = screen.getByRole('button', { name: 'Cancel' });
    expect(cancel).toBeDisabled();

    // Busy also blocks Escape-style closes routed through handleClose.
    fireEvent.click(cancel);
    expect(onClose).not.toHaveBeenCalled();
  });
});
