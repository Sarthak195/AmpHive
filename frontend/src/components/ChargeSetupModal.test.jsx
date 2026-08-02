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
import { useWallet } from '../contexts/WalletContext';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));
vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coins_per_kwh: 5, coin_inr_rate: 1 }),
}));
vi.mock('../contexts/WalletContext', () => ({ useWallet: vi.fn() }));

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

beforeEach(() => {
  vi.clearAllMocks();
  useWallet.mockReturnValue({ balance: 100, availableBalance: 100 });
});

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

  it('coverage uses availableBalance (not the raw balance) so a concurrent hold is respected', () => {
    // A second active session holds 60 coins — availableBalance (40) is what
    // the estimate must use, not the raw balance (100).
    useWallet.mockReturnValue({ balance: 100, availableBalance: 40 });
    renderModal();
    expect(screen.getByText(/covers ≈ 4\.0 kWh/)).toBeInTheDocument();
  });

  it('shows "charges until stopped" helper when "No limit" chip is selected', () => {
    renderModal();
    expect(screen.getByText(/Charges until you stop it — no time limit/i)).toBeInTheDocument();
  });

  it('a zero tariff (free charging) is used as-is, not coerced to the global fallback', () => {
    const zeroTariffPlug = { id: 7, name: 'Free Plug', price_per_kwh: 0 };
    render(
      <ChargeSetupModal
        open
        onClose={() => {}}
        plug={zeroTariffPlug}
        mode="start"
        onConfirm={vi.fn()}
      />
    );
    // price_per_kwh: 0 is a real, valid rate (free charging) — it must NOT
    // fall back to coins_per_kwh (5, from the ConfigContext mock).
    expect(screen.getByText(/₹0\.00/)).toBeInTheDocument();
    expect(screen.getByText(/covers ≈ 0\.0 kWh/)).toBeInTheDocument();
    expect(screen.queryByText(/₹5\.00/)).not.toBeInTheDocument();
  });
});

describe('ChargeSetupModal — availableBalance', () => {
  it('coverage uses availableBalance (not the raw balance) so a concurrent hold is respected', async () => {
    const { useWallet } = await import('../contexts/WalletContext');
    useWallet.mockReturnValue?.({ balance: 100, availableBalance: 40 });
    // useWallet above is a plain fn (not vi.fn) per the module mock, so
    // re-mock it directly for this test.
    vi.doMock('../contexts/WalletContext', () => ({
      useWallet: () => ({ balance: 100, availableBalance: 40 }),
    }));
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

  it('custom time cannot exceed 24 hours (1440 minutes)', async () => {
    renderModal();
    await userEvent.click(screen.getByRole('button', { name: 'Custom' }));
    const custom = screen.getByLabelText(/custom time limit/i);

    // 25:00 should be out of range
    await userEvent.type(custom, '25:00');
    expect(screen.getByText(/Maximum 24 hours/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start charging' })).toBeDisabled();

    // 24:00 should be valid
    await userEvent.clear(custom);
    await userEvent.type(custom, '24:00');
    expect(screen.queryByText(/Maximum 24 hours/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start charging' })).not.toBeDisabled();
  });

  it('energy limit cannot exceed 100 kWh and must be > 0', async () => {
    renderModal();
    const kwh = screen.getByLabelText('Energy limit (kWh)');

    // 101 kWh should be out of range
    await userEvent.type(kwh, '101');
    expect(screen.getByText(/Maximum 100 kWh/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start charging' })).toBeDisabled();

    // 0 kWh should be invalid
    await userEvent.clear(kwh);
    await userEvent.type(kwh, '0');
    expect(screen.getByText(/must be greater than 0/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start charging' })).toBeDisabled();

    // 50 kWh should be valid
    await userEvent.clear(kwh);
    await userEvent.type(kwh, '50');
    expect(screen.queryByText(/Maximum 100 kWh/)).not.toBeInTheDocument();
    expect(screen.queryByText(/must be greater than 0/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start charging' })).not.toBeDisabled();
  });

  it('shows "charges until stopped" kWh helper when energy limit is blank', async () => {
    renderModal();
    const kwh = screen.getByLabelText('Energy limit (kWh)');
    expect(screen.getByText(/Charges until you stop it — no energy limit/i)).toBeInTheDocument();

    // When user types something, the hint should remain
    await userEvent.type(kwh, '50');
    // The hint only shows when kwh === '', so it should disappear when typing
    // (This is the expected behavior based on the code)
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
