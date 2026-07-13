/**
 * ChargeSetupModal tests: tapping a charger opens this panel (charging does
 * NOT start instantly); the optional timer + kWh fields map to
 * max_duration_seconds / max_kwh, and both-blank starts with no limit.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';

import ChargeSetupModal from './ChargeSetupModal';

const PLUG = { id: 7, name: 'Tower B — P2', price_per_kwh: 5 };

const renderModal = (onStart = vi.fn().mockResolvedValue({})) => {
  render(<ChargeSetupModal plug={PLUG} rate={5} balance={100} onStart={onStart} onClose={vi.fn()} />);
  return onStart;
};

const clickStart = async () => {
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: /Start charging/ }));
  });
};

describe('ChargeSetupModal', () => {
  it('renders the charger name + price and both optional fields', () => {
    renderModal();
    expect(screen.getByRole('heading', { name: /Set up your charge/i })).toBeInTheDocument();
    expect(screen.getByText('Tower B — P2')).toBeInTheDocument();
    expect(screen.getByText(/5 coins\/kWh/)).toBeInTheDocument();
    expect(screen.getByLabelText(/Timer/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/kWh to dispense/i)).toBeInTheDocument();
  });

  it('starts with NO limit when both fields are blank', async () => {
    const onStart = renderModal();
    await clickStart();
    expect(onStart).toHaveBeenCalledWith(7, null);
  });

  it('sends max_kwh when only kWh is set', async () => {
    const onStart = renderModal();
    fireEvent.change(screen.getByLabelText(/kWh to dispense/i), { target: { value: '5' } });
    await clickStart();
    expect(onStart).toHaveBeenCalledWith(7, { max_kwh: 5 });
  });

  it('sends max_duration_seconds when only the timer is set (hours → seconds)', async () => {
    const onStart = renderModal();
    fireEvent.change(screen.getByLabelText(/Timer/i), { target: { value: '2' } });
    await clickStart();
    expect(onStart).toHaveBeenCalledWith(7, { max_duration_seconds: 7200 });
  });

  it('sends both when both are set', async () => {
    const onStart = renderModal();
    fireEvent.change(screen.getByLabelText(/Timer/i), { target: { value: '1' } });
    fireEvent.change(screen.getByLabelText(/kWh to dispense/i), { target: { value: '3' } });
    await clickStart();
    expect(onStart).toHaveBeenCalledWith(7, { max_kwh: 3, max_duration_seconds: 3600 });
  });

  it('surfaces an error from onStart without closing', async () => {
    const onStart = vi.fn().mockRejectedValue(new Error('Insufficient balance'));
    renderModal(onStart);
    await clickStart();
    expect(await screen.findByText(/Insufficient balance/)).toBeInTheDocument();
  });
});
