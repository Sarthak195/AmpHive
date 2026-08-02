/**
 * CpoPricingSimple tests: the one-input Simple pricing view in isolation —
 * prefill, the live example-cost line, the "custom schedule active" banner
 * for a non-uniform tenant, direct-save vs. confirm-before-overwrite, basic
 * validation, and the Advanced hand-off.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoPricingSimple from './CpoPricingSimple';

const UNIFORM_SUMMARY = { uniform: true, price: 5, tariffId: 1 };
const CUSTOM_SUMMARY = { uniform: false, price: null, tariffId: 1 };

const renderSimple = (props = {}) =>
  render(
    <CpoPricingSimple
      pricingSummary={UNIFORM_SUMMARY}
      seedPrice="5"
      coinInrRate={1}
      busy={false}
      error=""
      onSave={vi.fn().mockResolvedValue(true)}
      onSwitchToAdvanced={vi.fn()}
      {...props}
    />
  );

describe('uniform (default) state', () => {
  it('prefills the field and shows no custom-schedule banner', () => {
    renderSimple();
    expect(screen.getByLabelText('Price per kWh (₹)')).toHaveValue(5);
    expect(screen.queryByText('Custom schedule active.')).not.toBeInTheDocument();
  });

  it('shows a live example cost for a 3 kW / 1 h charge', async () => {
    renderSimple();
    // seed price 5 -> 3 kWh * 5 = 15 coins = ₹15.00 at rate 1
    expect(screen.getByText(/a 3 kW charge for 1 hour/)).toHaveTextContent('₹15.00');

    await userEvent.clear(screen.getByLabelText('Price per kWh (₹)'));
    await userEvent.type(screen.getByLabelText('Price per kWh (₹)'), '10');
    expect(screen.getByText(/a 3 kW charge for 1 hour/)).toHaveTextContent('₹30.00');
  });

  it('saves directly (no confirmation) when already uniform', async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    renderSimple({ onSave });
    await userEvent.click(screen.getByRole('button', { name: 'Save price' }));
    expect(onSave).toHaveBeenCalledWith(5);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('rejects an out-of-range price before calling onSave', async () => {
    const onSave = vi.fn();
    renderSimple({ onSave });
    await userEvent.clear(screen.getByLabelText('Price per kWh (₹)'));
    await userEvent.type(screen.getByLabelText('Price per kWh (₹)'), '0');
    await userEvent.click(screen.getByRole('button', { name: 'Save price' }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(/enter a rate/i);
  });

  it('routes external save errors into the inline error area', () => {
    renderSimple({ error: 'Server exploded.' });
    expect(screen.getByRole('alert')).toHaveTextContent('Server exploded.');
  });

  it('hands off to Advanced', async () => {
    const onSwitchToAdvanced = vi.fn();
    renderSimple({ onSwitchToAdvanced });
    await userEvent.click(screen.getByRole('button', { name: 'Switch to Advanced' }));
    expect(onSwitchToAdvanced).toHaveBeenCalled();
  });
});

describe('custom (non-uniform) state', () => {
  it('shows the custom-schedule banner instead of pretending one number applies everywhere', () => {
    renderSimple({ pricingSummary: CUSTOM_SUMMARY, seedPrice: '8' });
    expect(screen.getByText('Custom schedule active.')).toBeInTheDocument();
  });

  it('warns before overwriting, and only saves on confirm', async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    renderSimple({ pricingSummary: CUSTOM_SUMMARY, seedPrice: '8', onSave });

    await userEvent.click(screen.getByRole('button', { name: 'Save price' }));
    expect(onSave).not.toHaveBeenCalled();
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('Replace the custom schedule with a flat rate?');
    expect(dialog).toHaveTextContent(/different rates by charger, group, or time of day/i);

    await userEvent.click(screen.getByRole('button', { name: 'Replace with flat rate' }));
    expect(onSave).toHaveBeenCalledWith(8);
  });

  it('cancelling the warning does not save', async () => {
    const onSave = vi.fn();
    renderSimple({ pricingSummary: CUSTOM_SUMMARY, seedPrice: '8', onSave });

    await userEvent.click(screen.getByRole('button', { name: 'Save price' }));
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

it('renders nothing while the summary is still resolving', () => {
  const { container } = renderSimple({ pricingSummary: null });
  expect(container).toBeEmptyDOMElement();
});
