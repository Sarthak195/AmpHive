/**
 * Tabs tests: tablist/tab roles with aria-selected + roving tabindex, count
 * badges via .count-pill, click selection, and arrow-key navigation (with
 * wrap-around) moving both selection and focus.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import Tabs from './Tabs';

const tabs = [
  { id: 'sessions', label: 'Sessions', count: 3 },
  { id: 'ledger', label: 'Ledger' },
  { id: 'disputes', label: 'Disputes', count: 0 },
];

describe('Tabs', () => {
  it('renders roles, selection state and count pills', () => {
    render(<Tabs tabs={tabs} active="sessions" onChange={vi.fn()} ariaLabel="History views" />);

    expect(screen.getByRole('tablist', { name: 'History views' })).toBeInTheDocument();
    const all = screen.getAllByRole('tab');
    expect(all).toHaveLength(3);

    const active = screen.getByRole('tab', { name: /Sessions/ });
    expect(active).toHaveAttribute('aria-selected', 'true');
    expect(active).toHaveAttribute('tabindex', '0');
    expect(screen.getByRole('tab', { name: 'Ledger' })).toHaveAttribute('tabindex', '-1');

    // Counts render as pills (including an explicit 0).
    expect(active.querySelector('.count-pill')).toHaveTextContent('3');
    expect(screen.getByRole('tab', { name: /Disputes/ }).querySelector('.count-pill')).toHaveTextContent('0');
  });

  it('selects on click', () => {
    const onChange = vi.fn();
    render(<Tabs tabs={tabs} active="sessions" onChange={onChange} ariaLabel="History views" />);
    fireEvent.click(screen.getByRole('tab', { name: 'Ledger' }));
    expect(onChange).toHaveBeenCalledWith('ledger');
  });

  it('moves selection with arrow keys and wraps at the ends', () => {
    const onChange = vi.fn();
    render(<Tabs tabs={tabs} active="disputes" onChange={onChange} ariaLabel="History views" />);
    // Keys are dispatched at the selected tab, which is where the roving
    // tabindex puts focus in a real browser — the handler lives there.
    const focused = screen.getByRole('tab', { name: /Disputes/ });

    // ArrowRight from the last tab wraps to the first.
    fireEvent.keyDown(focused, { key: 'ArrowRight' });
    expect(onChange).toHaveBeenLastCalledWith('sessions');

    // ArrowLeft steps back.
    fireEvent.keyDown(focused, { key: 'ArrowLeft' });
    expect(onChange).toHaveBeenLastCalledWith('ledger');

    // Home/End jump to the edges.
    fireEvent.keyDown(focused, { key: 'Home' });
    expect(onChange).toHaveBeenLastCalledWith('sessions');
  });

  it('wires the selected tab to the panel it controls', () => {
    render(
      <Tabs tabs={tabs} active="ledger" onChange={vi.fn()} ariaLabel="History views">
        <p>Ledger rows</p>
      </Tabs>,
    );

    // The panel is named by its tab, so a screen reader reaching it announces
    // "Ledger, tab panel" rather than an anonymous region.
    const panel = screen.getByRole('tabpanel', { name: 'Ledger' });
    expect(panel).toHaveTextContent('Ledger rows');

    // ...and the tab points back at that exact panel.
    const selected = screen.getByRole('tab', { name: 'Ledger' });
    expect(selected).toHaveAttribute('aria-controls', panel.getAttribute('id'));

    // Inactive tabs must NOT claim to control it — their panel isn't mounted,
    // so an aria-controls here would be a dangling reference.
    expect(screen.getByRole('tab', { name: /Sessions/ })).not.toHaveAttribute('aria-controls');
  });
});
