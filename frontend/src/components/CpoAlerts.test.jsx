/**
 * CpoAlerts tests: only unacked critical/warning events render (info stays on
 * Health), critical sorts first with human copy from eventTypeCopy,
 * Acknowledge POSTs the ack + drops the banner + refreshes TenantContext,
 * an ack failure surfaces a toast without dropping the banner, a failed
 * fetch shows a retryable error line (never silently nothing), and the
 * strip caps at 4 banners with a "view all" link to /cpo/health.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import CpoAlerts from './CpoAlerts';
import api from '../api/client';
import { useTenant } from '../contexts/TenantContext';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));
vi.mock('../contexts/SessionContext', () => ({
  useSession: () => ({ alarms: [] }),
}));
vi.mock('../contexts/TenantContext', () => ({
  useTenant: vi.fn(),
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('./ui', () => ({
  useToast: () => toast,
}));

const EVENTS = [
  {
    id: 1,
    gateway_id: 'gw-01',
    plug_id: 3,
    event_type: 'UNAUTHORIZED_ON',
    severity: 'warning',
    detail: 'Plug switched on with no session',
    acknowledged: false,
    created_at: new Date().toISOString(),
  },
  {
    id: 2,
    gateway_id: 'gw-01',
    plug_id: null,
    event_type: 'OVERCURRENT_CUTOFF',
    severity: 'critical',
    detail: null,
    acknowledged: false,
    created_at: new Date().toISOString(),
  },
  {
    id: 3,
    gateway_id: 'gw-02',
    plug_id: null,
    event_type: 'OTA_STARTED',
    severity: 'info',
    detail: 'Firmware 2.3.0',
    acknowledged: false,
    created_at: new Date().toISOString(),
  },
];

const refresh = vi.fn();

const renderAlerts = () =>
  render(
    <MemoryRouter>
      <CpoAlerts />
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
  useTenant.mockReturnValue({ profile: null, counts: {}, loading: false, refresh });
  api.get.mockResolvedValue(EVENTS);
  api.post.mockResolvedValue({});
});

describe('CpoAlerts', () => {
  it('renders nothing when there are no unacked critical/warning events', async () => {
    api.get.mockResolvedValue([EVENTS[2]]); // info only
    const { container } = renderAlerts();
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith('/api/cpo/events?unacknowledged_only=true&limit=50')
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('shows critical/warning banners (critical first) with human event copy and a view-all link', async () => {
    renderAlerts();

    // Human copy via eventTypeCopy — never the raw enum.
    expect(await screen.findByText('Current safety cutoff')).toBeInTheDocument();
    expect(screen.getByText('Unauthorized power-on')).toBeInTheDocument();
    expect(screen.queryByText(/OVERCURRENT_CUTOFF/)).not.toBeInTheDocument();
    // Info-level events don't interrupt every page.
    expect(screen.queryByText(/firmware update started/i)).not.toBeInTheDocument();

    // Critical sorts above warning.
    const banners = screen.getAllByRole('button', { name: 'Acknowledge' });
    expect(banners).toHaveLength(2);
    const strip = screen.getByLabelText('Active alerts');
    const text = strip.textContent;
    expect(text.indexOf('Current safety cutoff')).toBeLessThan(
      text.indexOf('Unauthorized power-on')
    );

    // Fallback detail line uses the gateway; plug id appears when present.
    expect(screen.getByText(/Gateway gw-01/)).toBeInTheDocument();
    expect(screen.getByText(/Plug 3/)).toBeInTheDocument();

    expect(screen.getByRole('link', { name: /view all alerts/i })).toHaveAttribute(
      'href',
      '/cpo/health'
    );
  });

  it('Acknowledge POSTs the ack, removes the banner and refreshes the tenant badges', async () => {
    renderAlerts();
    await screen.findByText('Current safety cutoff');

    await userEvent.click(screen.getAllByRole('button', { name: 'Acknowledge' })[0]);

    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/cpo/events/2/ack', {}));
    await waitFor(() =>
      expect(screen.queryByText('Current safety cutoff')).not.toBeInTheDocument()
    );
    expect(screen.getByText('Unauthorized power-on')).toBeInTheDocument();
    expect(refresh).toHaveBeenCalled();
  });

  it('a failed ack surfaces a toast and keeps the banner', async () => {
    api.post.mockRejectedValue(new Error('nope'));
    renderAlerts();
    await screen.findByText('Current safety cutoff');

    await userEvent.click(screen.getAllByRole('button', { name: 'Acknowledge' })[0]);

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(screen.getByText('Current safety cutoff')).toBeInTheDocument();
    expect(refresh).not.toHaveBeenCalled();
  });

  it('a failed fetch shows a retryable error line, and Retry recovers', async () => {
    api.get.mockRejectedValueOnce(new Error('down'));
    renderAlerts();

    expect(await screen.findByText(/couldn't check for new alerts/i)).toBeInTheDocument();

    api.get.mockResolvedValue(EVENTS);
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('Current safety cutoff')).toBeInTheDocument();
  });

  it('caps the strip at 4 banners and counts the rest in the view-all link', async () => {
    api.get.mockResolvedValue(
      Array.from({ length: 6 }, (_, i) => ({
        id: i + 10,
        gateway_id: 'gw-01',
        plug_id: null,
        event_type: 'THERMAL_CUTOFF',
        severity: 'critical',
        detail: `Overheat ${i}`,
        acknowledged: false,
        created_at: new Date().toISOString(),
      }))
    );
    renderAlerts();

    await screen.findAllByRole('button', { name: 'Acknowledge' });
    expect(screen.getAllByRole('button', { name: 'Acknowledge' })).toHaveLength(4);
    expect(screen.getByRole('link', { name: 'View all 6 alerts' })).toHaveAttribute(
      'href',
      '/cpo/health'
    );
  });
});
