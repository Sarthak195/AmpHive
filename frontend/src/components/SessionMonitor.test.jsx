/**
 * SessionMonitor tests (redesign v3, C4): the ₹-first ring hero (determinate
 * vs indeterminate), the tariff-preview rate line (hidden on failure), the
 * stale/alarm/low-balance banners, the ConfirmDialog-gated stop with the
 * kWh/₹ estimate, and the mid-session limit editor PATCH.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

import SessionMonitor from './SessionMonitor';
import { useSession } from '../contexts/SessionContext';
import { useWallet } from '../contexts/WalletContext';
import api from '../api/client';

vi.mock('../contexts/SessionContext', () => ({ useSession: vi.fn() }));
vi.mock('../contexts/WalletContext', () => ({ useWallet: vi.fn() }));
vi.mock('../contexts/ConfigContext', () => ({
  useConfig: () => ({ coins_per_kwh: 5, coin_inr_rate: 1 }),
}));
vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn() },
}));

const baseData = {
  plug_id: 2,
  plug_name: 'Garage plug',
  power_w: 1000,
  energy_kwh: 0.25,
  current_a: 4.3,
  voltage_v: 231,
  cost_coins: 1.25,
  relay_on: true,
  duration_sec: 60,
};

const baseSession = (overrides = {}) => ({
  sessionData: { ...baseData, is_stale: false },
  sessionId: 7,
  isActive: true,
  stopSession: vi.fn().mockResolvedValue({}),
  updateLimits: vi.fn(),
  lastFrameAt: Date.now(),
  focusedStartedAt: new Date().toISOString(),
  focusedLimits: null,
  alarms: [],
  ...overrides,
});

const renderMonitor = async () => {
  const utils = render(
    <MemoryRouter>
      <SessionMonitor />
    </MemoryRouter>
  );
  // Flush the tariff-preview fetch promise.
  await act(async () => {});
  return utils;
};

beforeEach(() => {
  vi.clearAllMocks();
  useWallet.mockReturnValue({ balance: 1000, availableBalance: 1000 });
  api.get.mockResolvedValue({ base_price_per_kwh: 6, price_now: 6, slots: [] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('SessionMonitor — hero + meters', () => {
  it('renders the ₹ cost hero, kWh, the meter row and the Charging status', async () => {
    useSession.mockReturnValue(baseSession());
    await renderMonitor();
    expect(screen.getByText('Charging')).toBeInTheDocument();
    expect(screen.getByText('₹1.25')).toBeInTheDocument(); // cost, ₹-first
    expect(screen.getByText('0.25 kWh')).toBeInTheDocument();
    expect(screen.getByText('1.0 kW')).toBeInTheDocument(); // power now
    expect(screen.getByText('Garage plug')).toBeInTheDocument();
  });

  it('announces the live cost in an sr-only aria-live region', async () => {
    useSession.mockReturnValue(baseSession());
    await renderMonitor();
    const region = document.querySelector('[aria-live="polite"].sr-only');
    expect(region).toHaveTextContent('Current cost 1 rupees, 0.25 kilowatt hours');
  });

  it('shows the plug rate line from the tariff preview — never the config rate', async () => {
    useSession.mockReturnValue(baseSession());
    await renderMonitor();
    expect(api.get).toHaveBeenCalledWith('/api/plugs/2/tariff-preview');
    expect(screen.getByText('₹6.00')).toBeInTheDocument();
    expect(screen.getByText(/\/kWh now/)).toBeInTheDocument();
  });

  it('hides the rate line when the tariff preview fails (e.g. 404)', async () => {
    api.get.mockRejectedValue(Object.assign(new Error('Not found'), { status: 404 }));
    useSession.mockReturnValue(baseSession());
    await renderMonitor();
    expect(screen.queryByText(/\/kWh now/)).not.toBeInTheDocument();
  });
});

describe('SessionMonitor — ring', () => {
  it('is indeterminate (no progressbar) without a limit', async () => {
    useSession.mockReturnValue(baseSession({ focusedLimits: null }));
    await renderMonitor();
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
  });

  it('is determinate toward the kWh limit when one is set', async () => {
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, energy_kwh: 0.42, is_stale: false },
        focusedLimits: { max_kwh: 1.0, max_duration_seconds: null },
      })
    );
    await renderMonitor();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '42');
    expect(screen.getByText(/stops automatically/)).toBeInTheDocument();
    expect(screen.getByText(/0\.42 \/ 1\.00 kWh/)).toBeInTheDocument();
  });
});

describe('SessionMonitor — notices', () => {
  it('shows the reconnecting banner when no frame has arrived recently', async () => {
    useSession.mockReturnValue(baseSession({ lastFrameAt: Date.now() - 30000 }));
    await renderMonitor();
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
    expect(screen.getByText(/Live readings paused/)).toBeInTheDocument();
  });

  it('honors the server-side is_stale flag even with a fresh frame timestamp', async () => {
    useSession.mockReturnValue(
      baseSession({ sessionData: { ...baseData, is_stale: true } })
    );
    await renderMonitor();
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
  });

  it('surfaces a gateway alarm for this plug through eventTypeCopy', async () => {
    useSession.mockReturnValue(
      baseSession({
        alarms: [
          {
            plug_id: 2,
            event_type: 'OVERCURRENT_CUTOFF',
            detail: 'Plug drew 18 A',
            received_at: Date.now(),
          },
        ],
      })
    );
    await renderMonitor();
    expect(screen.getByText('Current safety cutoff')).toBeInTheDocument();
    expect(screen.getByText(/Plug drew 18 A/)).toBeInTheDocument();
  });

  it('ignores an alarm for a different plug', async () => {
    useSession.mockReturnValue(
      baseSession({
        alarms: [
          { plug_id: 99, event_type: 'UNAUTHORIZED_ON', detail: 'other plug', received_at: Date.now() },
        ],
      })
    );
    await renderMonitor();
    expect(screen.queryByText(/other plug/)).not.toBeInTheDocument();
  });

  it('warns on low balance with a Top up link back to this session', async () => {
    useWallet.mockReturnValue({ balance: 12, availableBalance: 12 }); // cost 10 → ₹2 left, under the floor
    useSession.mockReturnValue(
      baseSession({ sessionData: { ...baseData, cost_coins: 10, is_stale: false } })
    );
    await renderMonitor();
    expect(screen.getByText(/Low balance/)).toBeInTheDocument();
    expect(screen.getByText(/charging continues while you top up/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Top up' })).toHaveAttribute(
      'href',
      '/wallet?next=/session'
    );
  });

  it('does not warn when the balance comfortably covers the accrued cost', async () => {
    useWallet.mockReturnValue({ balance: 1000, availableBalance: 1000 });
    useSession.mockReturnValue(
      baseSession({ sessionData: { ...baseData, cost_coins: 10, is_stale: false } })
    );
    await renderMonitor();
    expect(screen.queryByText(/Low balance/)).not.toBeInTheDocument();
  });

  it('uses availableBalance (not the raw balance) for the low-balance check, respecting a concurrent hold', async () => {
    // Raw balance looks comfortable, but a second session's hold leaves only
    // 12 coins actually available — the low-balance warning should still fire.
    useWallet.mockReturnValue({ balance: 1000, availableBalance: 12 });
    useSession.mockReturnValue(
      baseSession({ sessionData: { ...baseData, cost_coins: 10, is_stale: false } })
    );
    await renderMonitor();
    expect(screen.getByText(/Low balance/)).toBeInTheDocument();
  });
});

describe('SessionMonitor — stop', () => {
  it('confirms the kWh/₹ consequence before stopping', async () => {
    const stopSession = vi.fn().mockResolvedValue({});
    useSession.mockReturnValue(baseSession({ stopSession }));
    await renderMonitor();

    fireEvent.click(screen.getByRole('button', { name: 'Stop charging' }));
    const dialog = screen.getByRole('dialog', { name: 'Stop charging?' });
    expect(
      within(dialog).getByText(/You'll be billed for 0\.25 kWh — about ₹1\.25\./)
    ).toBeInTheDocument();
    expect(stopSession).not.toHaveBeenCalled();

    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Stop charging' }));
    });
    expect(stopSession).toHaveBeenCalled();
  });

  it('does not stop when the confirmation is cancelled', async () => {
    const stopSession = vi.fn();
    useSession.mockReturnValue(baseSession({ stopSession }));
    await renderMonitor();

    fireEvent.click(screen.getByRole('button', { name: 'Stop charging' }));
    const dialog = screen.getByRole('dialog', { name: 'Stop charging?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(stopSession).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
});

describe('SessionMonitor — limit editor', () => {
  it('opens prefilled and PATCHes the new target on save', async () => {
    const updateLimits = vi.fn().mockResolvedValue({
      status: 'updated',
      max_kwh: 2,
      max_duration_seconds: 1800,
    });
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, energy_kwh: 0.42, is_stale: false },
        updateLimits,
        focusedLimits: { max_kwh: 1.0, max_duration_seconds: 1800 },
      })
    );
    await renderMonitor();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText(/Energy limit/i), { target: { value: '2' } });
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: /Save limit/i }));
    });

    // 0.5 h stayed → 1800 s; new kWh sent alongside it.
    expect(updateLimits).toHaveBeenCalledWith(7, { max_kwh: 2, max_duration_seconds: 1800 });
  });

  it('rejects an empty limit edit without calling the API', async () => {
    const updateLimits = vi.fn();
    useSession.mockReturnValue(
      baseSession({
        updateLimits,
        focusedLimits: { max_kwh: 1.0, max_duration_seconds: 1800 },
      })
    );
    await renderMonitor();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText(/Energy limit/i), { target: { value: '' } });
    fireEvent.change(screen.getByLabelText(/Time limit/i), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /Save limit/i }));

    expect(screen.getByText(/Enter an energy/i)).toBeInTheDocument();
    expect(updateLimits).not.toHaveBeenCalled();
  });
});
