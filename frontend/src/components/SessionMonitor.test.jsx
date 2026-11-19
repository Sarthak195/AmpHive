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
import { toastBus } from './ui';
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
  focusedHoldCoins: null,
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

describe('SessionMonitor: idle fee terms', () => {
  it('shows the plug tariff idle fee and the unplug hint on the looks-full banner', async () => {
    api.get.mockResolvedValue({
      base_price_per_kwh: 6, price_now: 6, slots: [],
      idle_fee: { grace_min: 15, per_min: 2, cap: 30 },
    });
    useSession.mockReturnValue(
      baseSession({ sessionData: { ...baseData, is_stale: false, power_w: 0, charge_full: true } })
    );
    await renderMonitor();
    expect(screen.getByTestId('session-idle-fee')).toHaveTextContent(
      /₹2\.00\/min\s*after 15 min, at most ₹30\.00/
    );
    expect(screen.getByText(/Stop and unplug within 15 min to avoid the idle fee/)).toBeInTheDocument();
  });

  it('shows nothing when the tariff has no idle fee', async () => {
    useSession.mockReturnValue(baseSession());
    await renderMonitor();
    expect(screen.queryByTestId('session-idle-fee')).not.toBeInTheDocument();
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

  it('shows the car-looks-full banner once the backend flags charge_full', async () => {
    useSession.mockReturnValue(baseSession());
    const { rerender } = await renderMonitor();
    expect(screen.queryByText(/looks fully charged/)).not.toBeInTheDocument();
    useSession.mockReturnValue(
      baseSession({ sessionData: { ...baseData, is_stale: false, power_w: 0, charge_full: true } })
    );
    rerender(
      <MemoryRouter>
        <SessionMonitor />
      </MemoryRouter>
    );
    expect(screen.getByText(/looks fully charged/)).toBeInTheDocument();
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

  it('never shows an info-level notice (fw 2.8.0 PLUG_RECOVERED) as an alarm banner', async () => {
    useSession.mockReturnValue(
      baseSession({
        alarms: [
          {
            plug_id: 2,
            event_type: 'PLUG_RECOVERED',
            severity: 'info',
            detail: 'Plug reachable again after ~95 s.',
            received_at: Date.now(),
          },
        ],
      })
    );
    await renderMonitor();
    expect(screen.queryByText(/reachable again/)).not.toBeInTheDocument();
    expect(document.querySelector('.banner-danger')).toBeNull();
  });

  it('shows a PLUG_UNREACHABLE warning, and lets a newer PLUG_RECOVERED clear it', async () => {
    const unreachable = {
      plug_id: 2,
      event_type: 'PLUG_UNREACHABLE',
      severity: 'warning',
      detail: "Gateway couldn't get a complete reading from the plug 3 times in a row - readings may pause until it answers.",
      received_at: Date.now() - 5000,
    };
    useSession.mockReturnValue(baseSession({ alarms: [unreachable] }));
    const { unmount } = await renderMonitor();
    expect(screen.getByText('Plug unreachable')).toBeInTheDocument();
    unmount();

    // Newest first, as SessionContext keeps them.
    useSession.mockReturnValue(
      baseSession({
        alarms: [
          {
            plug_id: 2,
            event_type: 'PLUG_RECOVERED',
            severity: 'info',
            detail: 'Plug reachable again after ~5 s.',
            received_at: Date.now(),
          },
          unreachable,
        ],
      })
    );
    await renderMonitor();
    expect(screen.queryByText('Plug unreachable')).not.toBeInTheDocument();
    expect(document.querySelector('.banner-danger')).toBeNull();
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
    expect(screen.getByText(/charging continues while you add credit/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Add credit' })).toHaveAttribute(
      'href',
      '/credit?next=/session'
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

  it('falls back to availableBalance for a legacy session with no hold (hold_coins null)', async () => {
    // Raw balance looks comfortable, but a second session's hold leaves only
    // 12 coins actually available — the low-balance warning should still fire
    // for a legacy/unheld session, matching the backend's own fallback.
    useWallet.mockReturnValue({ balance: 1000, availableBalance: 12 });
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, cost_coins: 10, is_stale: false },
        focusedHoldCoins: null,
      })
    );
    await renderMonitor();
    expect(screen.getByText(/Low balance/)).toBeInTheDocument();
  });

  it('uses the session’s own hold_coins (not availableBalance) as the threshold when the session has a hold', async () => {
    // The wallet looks comfortable, but THIS session's own authorization
    // hold — the backend's actual auto-stop threshold — is nearly exhausted.
    useWallet.mockReturnValue({ balance: 1000, availableBalance: 1000 });
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, cost_coins: 9, is_stale: false },
        focusedHoldCoins: 10,
      })
    );
    await renderMonitor();
    expect(screen.getByText(/Low balance/)).toBeInTheDocument();
  });

  it('does not warn about low balance when the driver’s own kWh limit, not the wallet, will stop the session', async () => {
    // Hold sized to the limit (10 kWh x 5 = 50) on a well-funded wallet: the
    // hold running down only means the limit is near.
    api.get.mockResolvedValue({ base_price_per_kwh: 5, price_now: 5, slots: [] });
    useWallet.mockReturnValue({ balance: 1000, availableBalance: 950 });
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, energy_kwh: 9.5, cost_coins: 47.5, is_stale: false },
        focusedHoldCoins: 50,
        focusedLimits: { max_kwh: 10, max_duration_seconds: null },
      })
    );
    await renderMonitor();
    expect(screen.queryByText(/Low balance/)).not.toBeInTheDocument();
  });

  it('still warns when the wallet capped the hold below the cost of the kWh limit', async () => {
    // Limit 10 kWh would cost 50, but the wallet only covered a 20-coin hold.
    api.get.mockResolvedValue({ base_price_per_kwh: 5, price_now: 5, slots: [] });
    useWallet.mockReturnValue({ balance: 20, availableBalance: 0 });
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, energy_kwh: 3.6, cost_coins: 18, is_stale: false },
        focusedHoldCoins: 20,
        focusedLimits: { max_kwh: 10, max_duration_seconds: null },
      })
    );
    await renderMonitor();
    expect(screen.getByText(/Low balance/)).toBeInTheDocument();
  });

  it('does not warn when the session hold comfortably covers the accrued cost, even with a tight wallet', async () => {
    // The wallet is nearly empty, but this session reserved its own hold up
    // front — a sibling session's spending shouldn't false-trigger this one.
    useWallet.mockReturnValue({ balance: 12, availableBalance: 12 });
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, cost_coins: 1, is_stale: false },
        focusedHoldCoins: 100,
      })
    );
    await renderMonitor();
    expect(screen.queryByText(/Low balance/)).not.toBeInTheDocument();
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

  it('blocks a kWh target below what has already been consumed', async () => {
    const updateLimits = vi.fn();
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, energy_kwh: 0.5, is_stale: false },
        updateLimits,
        focusedLimits: { max_kwh: 1.0, max_duration_seconds: null },
      })
    );
    await renderMonitor();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText(/Energy limit/i), { target: { value: '0.2' } });
    fireEvent.click(screen.getByRole('button', { name: /Save limit/i }));

    expect(screen.getByText(/already used/i)).toBeInTheDocument();
    expect(updateLimits).not.toHaveBeenCalled();
  });

  it('blocks a time target below the elapsed time', async () => {
    const updateLimits = vi.fn();
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, is_stale: false },
        focusedStartedAt: new Date(Date.now() - 3600 * 1000).toISOString(), // 1 h elapsed
        updateLimits,
        focusedLimits: { max_kwh: null, max_duration_seconds: 7200 },
      })
    );
    await renderMonitor();

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    // 0.25 h (15 min) is below the ~1 h already elapsed.
    fireEvent.change(screen.getByLabelText(/Time limit/i), { target: { value: '0.25' } });
    fireEvent.click(screen.getByRole('button', { name: /Save limit/i }));

    expect(screen.getByText(/elapsed time/i)).toBeInTheDocument();
    expect(updateLimits).not.toHaveBeenCalled();
  });
});

describe('SessionMonitor: page open while the gateway is at its 10 s idle cadence', () => {
  it('shows the REST numbers at once with an "Updating" hint before the first frame, not a paused banner', async () => {
    useSession.mockReturnValue(
      baseSession({
        sessionData: { plug_id: 2, plug_name: 'Garage plug', status: 'charging', energy_kwh: 1.2, cost_coins: 7 },
        lastFrameAt: null,
        watchSince: Date.now(),
      })
    );
    await renderMonitor();
    expect(screen.getByText('₹7.00')).toBeInTheDocument();
    expect(screen.getByText('1.20 kWh')).toBeInTheDocument();
    expect(screen.getByText('Updating…')).toBeInTheDocument();
    expect(screen.queryByText(/Live readings paused/)).not.toBeInTheDocument();
    expect(screen.queryByText('0 W')).not.toBeInTheDocument();
  });

  it('treats a stale or old first cached frame as updating while the gateway wakes', async () => {
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, is_stale: true, age_sec: 40 },
        watchSince: Date.now() - 2000,
      })
    );
    await renderMonitor();
    expect(screen.getByText('₹1.25')).toBeInTheDocument();
    expect(screen.getByText('Updating…')).toBeInTheDocument();
    expect(screen.queryByText('Reconnecting…')).not.toBeInTheDocument();
    expect(screen.queryByText(/Live readings paused/)).not.toBeInTheDocument();
  });

  it('shows "Charging" once a fresh frame arrives', async () => {
    useSession.mockReturnValue(
      baseSession({
        sessionData: { ...baseData, is_stale: false, age_sec: 0.4 },
        watchSince: Date.now() - 3000,
        lastFrameAt: Date.now(),
      })
    );
    await renderMonitor();
    expect(screen.getByText('Charging')).toBeInTheDocument();
  });

  it('falls back to the paused banner once the wake-up window passes with no frame', async () => {
    useSession.mockReturnValue(
      baseSession({ lastFrameAt: null, watchSince: Date.now() - 20000 })
    );
    await renderMonitor();
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument();
    expect(screen.getByText(/Live readings paused/)).toBeInTheDocument();
  });

  it('puts the stopped session bill in the toast (a sibling may take focus, with no receipt)', async () => {
    const okSpy = vi.spyOn(toastBus, 'ok');
    const stopSession = vi.fn().mockResolvedValue({ coins_spent: 4.5, energy_kwh: 0.9 });
    useSession.mockReturnValue(baseSession({ stopSession }));
    await renderMonitor();
    fireEvent.click(screen.getByRole('button', { name: 'Stop charging' }));
    const dialog = screen.getByRole('dialog', { name: 'Stop charging?' });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Stop charging' }));
    });
    expect(okSpy).toHaveBeenCalledWith(expect.stringMatching(/Billed 0\.90 kWh, ₹4\.50/));
    okSpy.mockRestore();
  });
});
