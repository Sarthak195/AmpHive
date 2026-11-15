/**
 * StarRating tests: display mode announces the value via aria-label and is
 * non-interactive; input mode (onChange present) renders 5 radio buttons and
 * reports the clicked value.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import StarRating from './StarRating';

describe('StarRating', () => {
  it('display mode announces the rating and has no buttons', () => {
    render(<StarRating value={4.5} />);
    expect(screen.getByRole('img', { name: /4.5 out of 5/i })).toBeInTheDocument();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  });

  it('input mode renders 5 radios and reports the clicked value', async () => {
    const onChange = vi.fn();
    render(<StarRating value={0} onChange={onChange} />);
    const radios = screen.getAllByRole('radio');
    expect(radios).toHaveLength(5);
    await userEvent.click(screen.getByRole('radio', { name: '4 stars' }));
    expect(onChange).toHaveBeenCalledWith(4);
  });

  it('marks the selected star as checked', () => {
    render(<StarRating value={3} onChange={() => {}} />);
    expect(screen.getByRole('radio', { name: '3 stars' })).toHaveAttribute(
      'aria-checked',
      'true'
    );
  });
});
