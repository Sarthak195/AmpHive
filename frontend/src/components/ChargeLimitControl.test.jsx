/**
 * ChargeLimitControl tests: collapsed by default, presets/custom input emit
 * the raw spec upward, the coins→kWh conversion (computeChargeLimits) with
 * plug-rate + config-fallback + backend-bounds clamping, and the derived-kWh
 * preview for the ₹/coins mode.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ChargeLimitControl from './ChargeLimitControl';
import { computeChargeLimits, formatDuration } from '../utils/chargeLimits';

describe('computeChargeLimits', () => {
  it('returns null for no spec (control closed / never opened)', () => {
    expect(computeChargeLimits(null, 5)).toBeNull();
  });

  it('returns null for empty or invalid values', () => {
    expect(computeChargeLimits({ mode: 'kwh', value: '' }, 5)).toBeNull();
    expect(computeChargeLimits({ mode: 'kwh', value: 'abc' }, 5)).toBeNull();
    expect(computeChargeLimits({ mode: 'kwh', value: '-2' }, 5)).toBeNull();
    expect(computeChargeLimits({ mode: 'kwh', value: '0' }, 5)).toBeNull();
  });

  it('maps a kWh value straight to max_kwh', () => {
    expect(computeChargeLimits({ mode: 'kwh', value: '1' }, 5)).toEqual({ max_kwh: 1 });
    expect(computeChargeLimits({ mode: 'kwh', value: '2.5' }, 5)).toEqual({ max_kwh: 2.5 });
  });

  it('clamps kWh into the backend bounds (0.1–100)', () => {
    expect(computeChargeLimits({ mode: 'kwh', value: '250' }, 5)).toEqual({ max_kwh: 100 });
    expect(computeChargeLimits({ mode: 'kwh', value: '0.01' }, 5)).toEqual({ max_kwh: 0.1 });
  });

  it('converts coins to kWh at the given rate', () => {
    // ₹50 at 10 coins/kWh → 5 kWh
    expect(computeChargeLimits({ mode: 'coins', value: '50' }, 10)).toEqual({ max_kwh: 5 });
    // rounds to 2 decimals: ₹10 at 3 coins/kWh → 3.333… → 3.33
    expect(computeChargeLimits({ mode: 'coins', value: '10' }, 3)).toEqual({ max_kwh: 3.33 });
  });

  it('falls back to the default 5 coins/kWh when the rate is unknown', () => {
    expect(computeChargeLimits({ mode: 'coins', value: '25' }, undefined)).toEqual({ max_kwh: 5 });
    expect(computeChargeLimits({ mode: 'coins', value: '25' }, 0)).toEqual({ max_kwh: 5 });
  });

  it('maps hours to max_duration_seconds, clamped to 24 h', () => {
    expect(computeChargeLimits({ mode: 'time', value: '0.5' }, 5)).toEqual({ max_duration_seconds: 1800 });
    expect(computeChargeLimits({ mode: 'time', value: '2' }, 5)).toEqual({ max_duration_seconds: 7200 });
    expect(computeChargeLimits({ mode: 'time', value: '30' }, 5)).toEqual({ max_duration_seconds: 86400 });
  });
});

describe('formatDuration', () => {
  it('renders h/min human-readably', () => {
    expect(formatDuration(1800)).toBe('30 min');
    expect(formatDuration(3600)).toBe('1 h');
    expect(formatDuration(5400)).toBe('1 h 30 min');
  });
});

describe('<ChargeLimitControl />', () => {
  it('is collapsed by default and emits null (no limit)', () => {
    const onChange = vi.fn();
    render(<ChargeLimitControl rate={5} onChange={onChange} />);

    const toggle = screen.getByRole('button', { name: /Set a charging limit/ });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByLabelText('Limit value')).not.toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith(null);
  });

  it('emits the chosen preset as a spec once opened', async () => {
    const onChange = vi.fn();
    render(<ChargeLimitControl rate={5} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: /Set a charging limit/ }));
    await userEvent.click(screen.getByRole('button', { name: '1 kWh' }));

    expect(onChange).toHaveBeenLastCalledWith({ mode: 'kwh', value: '1' });
    expect(screen.getByText('Stops automatically at 1.00 kWh.')).toBeInTheDocument();
  });

  it('emits a custom typed value', async () => {
    const onChange = vi.fn();
    render(<ChargeLimitControl rate={5} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: /Set a charging limit/ }));
    await userEvent.type(screen.getByLabelText('Limit value'), '2.5');

    expect(onChange).toHaveBeenLastCalledWith({ mode: 'kwh', value: '2.5' });
  });

  it('shows the derived kWh for a ₹/coins limit at the plug rate', async () => {
    const onChange = vi.fn();
    render(<ChargeLimitControl rate={10} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: /Set a charging limit/ }));
    await userEvent.click(screen.getByRole('button', { name: '₹ / coins' }));
    await userEvent.click(screen.getByRole('button', { name: '₹50' }));

    expect(onChange).toHaveBeenLastCalledWith({ mode: 'coins', value: '50' });
    expect(screen.getByText(/≈ 5\.00 kWh at 10 coins\/kWh/)).toBeInTheDocument();
  });

  it('resets the value when the mode changes (units differ)', async () => {
    const onChange = vi.fn();
    render(<ChargeLimitControl rate={5} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: /Set a charging limit/ }));
    await userEvent.click(screen.getByRole('button', { name: '5 kWh' }));
    await userEvent.click(screen.getByRole('button', { name: 'Time' }));

    expect(onChange).toHaveBeenLastCalledWith({ mode: 'time', value: '' });
    expect(screen.getByText(/No limit — charging stops at the standard safety caps/)).toBeInTheDocument();
  });

  it('shows the stop-after preview for a time limit', async () => {
    const onChange = vi.fn();
    render(<ChargeLimitControl rate={5} onChange={onChange} />);

    await userEvent.click(screen.getByRole('button', { name: /Set a charging limit/ }));
    await userEvent.click(screen.getByRole('button', { name: 'Time' }));
    await userEvent.click(screen.getByRole('button', { name: '30 min' }));

    expect(onChange).toHaveBeenLastCalledWith({ mode: 'time', value: '0.5' });
    expect(screen.getByText('Stops automatically after 30 min.')).toBeInTheDocument();
  });

  it('emits null again when collapsed after choosing a limit', async () => {
    const onChange = vi.fn();
    render(<ChargeLimitControl rate={5} onChange={onChange} />);

    const toggle = screen.getByRole('button', { name: /Set a charging limit/ });
    await userEvent.click(toggle);
    await userEvent.click(screen.getByRole('button', { name: '1 kWh' }));
    await userEvent.click(toggle); // collapse = no limit

    expect(onChange).toHaveBeenLastCalledWith(null);
  });
});
