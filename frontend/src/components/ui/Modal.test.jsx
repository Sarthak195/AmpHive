/**
 * Modal tests: dialog semantics (role/aria-modal/labelled by the title),
 * focus moves inside on open and Tab is trapped (cycles first↔last), Escape
 * and overlay click call onClose, focus returns to the trigger after close,
 * and sizes map to .modal-sm/.modal-lg.
 */
import { describe, it, expect, vi } from 'vitest';
import { useState } from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import Modal from './Modal';

// Harness with a real trigger so focus restoration is observable.
function Harness({ modalProps = {} }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button onClick={() => setOpen(true)}>Open modal</button>
      <Modal open={open} onClose={() => setOpen(false)} title="Example dialog" {...modalProps}>
        <p>Body copy</p>
        <button>First</button>
        <button>Last</button>
      </Modal>
    </div>
  );
}

describe('Modal', () => {
  it('renders nothing when closed and a labelled dialog when open', async () => {
    render(<Harness />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Open modal' }));

    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    // Labelled by its title.
    const titleId = dialog.getAttribute('aria-labelledby');
    expect(document.getElementById(titleId)).toHaveTextContent('Example dialog');
    expect(screen.getByText('Body copy')).toBeInTheDocument();
  });

  it('moves focus inside on open and traps Tab at the edges', async () => {
    render(<Harness />);
    await userEvent.click(screen.getByRole('button', { name: 'Open modal' }));

    const first = screen.getByRole('button', { name: 'First' });
    const last = screen.getByRole('button', { name: 'Last' });
    expect(first).toHaveFocus();

    // Tab past the last focusable wraps to the first.
    last.focus();
    await userEvent.tab();
    expect(first).toHaveFocus();

    // Shift+Tab before the first wraps to the last.
    await userEvent.tab({ shift: true });
    expect(last).toHaveFocus();
  });

  it('closes on Escape and restores focus to the trigger', async () => {
    render(<Harness />);
    const trigger = screen.getByRole('button', { name: 'Open modal' });
    await userEvent.click(trigger);
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it('closes on overlay click but not on clicks inside the dialog', async () => {
    const onClose = vi.fn();
    render(
      <Modal open onClose={onClose} title="Overlay test">
        <button>Inside</button>
      </Modal>
    );

    fireEvent.click(screen.getByText('Inside'));
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(document.querySelector('.overlay'));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('maps size to the modal-sm / modal-lg classes', () => {
    const { rerender } = render(
      <Modal open onClose={() => {}} title="Sized" size="sm">
        x
      </Modal>
    );
    expect(screen.getByRole('dialog')).toHaveClass('modal-sm');

    rerender(
      <Modal open onClose={() => {}} title="Sized" size="lg">
        x
      </Modal>
    );
    expect(screen.getByRole('dialog')).toHaveClass('modal-lg');

    rerender(
      <Modal open onClose={() => {}} title="Sized">
        x
      </Modal>
    );
    const dialog = screen.getByRole('dialog');
    expect(dialog).not.toHaveClass('modal-sm');
    expect(dialog).not.toHaveClass('modal-lg');
  });
});
