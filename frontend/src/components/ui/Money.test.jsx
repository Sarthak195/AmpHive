/**
 * Money component tests: ₹-first rendering from coins (at a rate) or a
 * direct inr amount; coins appear only as secondary copy when showCoins.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import Money from './Money';

describe('Money', () => {
  it('renders coins as ₹ at 1:1 by default, without coin copy', () => {
    render(<Money coins={100} />);
    expect(screen.getByText('₹100.00')).toBeInTheDocument();
    expect(screen.queryByText(/coins/)).not.toBeInTheDocument();
  });

  it('applies the rate when converting coins', () => {
    render(<Money coins={100} rate={1.5} />);
    expect(screen.getByText('₹150.00')).toBeInTheDocument();
  });

  it('renders a direct inr amount', () => {
    render(<Money inr={99.5} />);
    expect(screen.getByText('₹99.50')).toBeInTheDocument();
  });

  it('demotes coins to secondary copy when showCoins', () => {
    render(<Money coins={250} showCoins />);
    expect(screen.getByText(/₹250\.00/)).toBeInTheDocument();
    expect(screen.getByText('(250 coins)')).toBeInTheDocument();
  });

  it('renders a dash when no amount is given', () => {
    render(<Money />);
    expect(screen.getByText('—')).toBeInTheDocument();
  });
});
