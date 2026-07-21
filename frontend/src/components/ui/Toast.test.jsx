/**
 * Toast tests: useToast renders tone-classed toasts inside the aria-live
 * .toasts region, toasts auto-dismiss after 5s (fake timers), the manual
 * dismiss button removes one immediately, and the module-level toastBus
 * reaches a mounted provider from non-component code.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

import { ToastProvider, useToast, toastBus } from './Toast';

function Trigger() {
  const toast = useToast();
  return (
    <div>
      <button onClick={() => toast.ok('Saved')}>ok</button>
      <button onClick={() => toast.error('Broke')}>error</button>
      <button onClick={() => toast.warn('Careful')}>warn</button>
      <button onClick={() => toast.info('FYI')}>info</button>
    </div>
  );
}

const renderWithProvider = () =>
  render(
    <ToastProvider>
      <Trigger />
    </ToastProvider>
  );

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('Toast', () => {
  it('renders each tone in the polite live region', () => {
    renderWithProvider();

    fireEvent.click(screen.getByText('ok'));
    fireEvent.click(screen.getByText('error'));
    fireEvent.click(screen.getByText('warn'));
    fireEvent.click(screen.getByText('info'));

    const region = document.querySelector('.toasts');
    expect(region).toHaveAttribute('aria-live', 'polite');

    expect(screen.getByText('Saved').closest('.toast')).toHaveClass('toast-ok');
    expect(screen.getByText('Broke').closest('.toast')).toHaveClass('toast-danger');
    expect(screen.getByText('Careful').closest('.toast')).toHaveClass('toast-warn');
    expect(screen.getByText('FYI').closest('.toast')).toHaveClass('toast-info');
  });

  it('auto-dismisses after 5 seconds', () => {
    renderWithProvider();
    fireEvent.click(screen.getByText('ok'));
    expect(screen.getByText('Saved')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(4999);
    });
    expect(screen.getByText('Saved')).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.queryByText('Saved')).not.toBeInTheDocument();
  });

  it('dismisses on the manual dismiss button', () => {
    renderWithProvider();
    fireEvent.click(screen.getByText('ok'));

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss notification' }));
    expect(screen.queryByText('Saved')).not.toBeInTheDocument();
  });

  it('accepts pushes from the module-level toastBus', () => {
    renderWithProvider();

    act(() => {
      toastBus.error('Pushed from outside React');
    });
    expect(screen.getByText('Pushed from outside React').closest('.toast')).toHaveClass(
      'toast-danger'
    );
  });
});
