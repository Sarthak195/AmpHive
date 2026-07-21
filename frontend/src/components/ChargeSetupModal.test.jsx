/**
 * ChargeSetupModal tests — the shared start/queue setup dialog:
 * mode-specific copy, preset time chips + custom h:mm (no fractional hours),
 * the optional energy limit, coverage line from the plug's OWN price,
 * onConfirm(plugId, limits) payloads, inline errors from a throwing
 * onConfirm, and the circuit_full → "Ask for capacity" affordance.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import ChargeSetupModal from './ChargeSetupModal';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coins_per_kwh: 5, coin_inr_rate: 1 }),
}));
vi.mock('../contexts/WalletContext', () => ({
  useWallet: () => ({ balance: 100 }),
}));

const PLUG = { id: 7, name: 'Garage Plug', price_per_kwh: 10 };

const renderModal = (props = {}) => {
  const onConfirm = props.onConfirm || vi.fn().mockResolvedValue();
  const onClose = props.onClose || vi.fn();
  render(
    <ChargeSetupModal
      open
      onClose={onClose}
      plug={PLUG}
      mode={props.mode || 'start'}
      onConfirm={onConfirm}
    />
  );
  return { onConfirm, onClose };
};

beforeEach(() => vi.clearAllMocks());

describe('ChargeSetupModal — copy per mode', () => {
  it('start mode: "Set up your charge" / "Start charging"', () => {
    renderModal({ mode: 'start' });
    expect(screen.getByRole('dialog', { name: 'Set up your charge' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start charging' })).toBeInTheDocument();
  });

  it('queue mode: "Queue this charge" / "Join the queue" + auto-start copy', () => {
    renderModal({ mode: 'queue' });
    expect(screen.getByRole('dialog', { name: 'Queue this charge' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Join the queue' })).toBeInTheDocument();
    expect(screen.getByText(/starts automatically when the circuit has room/i)).toBeInTheDocument();
  });

  it("coverage line uses the plug's own price", () => {
    renderModal();
    // price 10, balance 100 → covers ≈ 10.0 kWh
    expect(screen.getByText(/₹10\.00/)).toBeInTheDocument();
    expect(screen.getByText(/covers ≈ 10\.0 kWh/)).toBeInTheDocument();
  });
});

describe('ChargeSetupModal — limits payload', () => {
  it('no limits chosen → onConfirm(plugId, null), then closes', async () => {
    const { onConfirm, onClose } = renderModal();
    await userEvent.click(screen.getByRole('button', { name: 'Start charging' }));
    expect(onConfirm).toHaveBeenCalledWith(7, null);
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it('preset chip + energy field → max_duration_seconds + max_kwh', async () => {
    const { onConfirm } = renderModal();
    await userEvent.click(screen.getByRole('button', { name: '2h' }));
    await userEvent.type(screen.getByLabelText('Energy limit (kWh)'), '5');
    await userEvent.click(screen.getByRole('button', { name: 'Start charging' }));
    expect(onConfirm).toHaveBeenCalledWith(7, { max_kwh: 5, max_duration_seconds: 7200 });
  });

  it('custom h:mm parses to seconds; an invalid value disables confirm', async () => {
    const { onConfirm } = renderModal();
    await userEvent.click(screen.getByRole('button', { name: 'Custom' }));
    const custom = screen.getByLabelText(/custom time limit/i);

    await userEvent.type(custom, 'abc');
    expect(screen.getByText(/hours:minutes, like 1:30/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start charging' })).toBeDisabled();

    await userEvent.clear(custom);
    await userEvent.type(custom, '1:30');
    await userEvent.click(screen.getByRole('button', { name: 'Start charging' }));
    expect(onConfirm).toHaveBeenCalledWith(7, { max_duration_seconds: 5400 });
  });
});

describe('ChargeSetupModal — failures', () => {
  it('a throwing onConfirm surfaces friendly copy inline and keeps the modal open', async () => {
    const err = Object.assign(new Error('offline'), { code: 'gateway_offline' });
    const { onClose } = renderModal({ onConfirm: vi.fn().mockRejectedValue(err) });
    await userEvent.click(screen.getByRole('button', { name: 'Start charging' }));

    expect(
      await screen.findByText("This charger can't be reached right now")
    ).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('circuit_full offers "Ask for capacity" which posts request-capacity', async () => {
    const err = Object.assign(new Error('full'), { code: 'circuit_full' });
    renderModal({ onConfirm: vi.fn().mockRejectedValue(err) });
    api.post.mockResolvedValue({});

    await userEvent.click(screen.getByRole('button', { name: 'Start charging' }));
    expect(
      await screen.findByText('This circuit is at capacity right now')
    ).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: 'Ask for capacity' }));
    expect(api.post).toHaveBeenCalledWith('/api/plugs/7/request-capacity');
    expect(
      await screen.findByText(/you'll get a notification when this circuit has room/i)
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Ask for capacity' })).not.toBeInTheDocument();
  });
});
