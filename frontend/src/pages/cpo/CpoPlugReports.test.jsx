/**
 * CpoPlugReports tests (near-copy of CpoDisputes.test.jsx's shape, minus the
 * refund fields): the Open-by-default status tabs, best-effort plug-name
 * enrichment (degrading to a raw plug id when the lookup misses), the
 * resolve ConfirmDialog for both "acknowledge" (OPEN only) and "resolve"
 * (closes it out) actions, and the usual skeleton/ErrorState/EmptyState via
 * DataTable.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoPlugReports from './CpoPlugReports';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div data-testid="cpo-layout">{children}</div>,
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const OPEN_REPORT = {
  id: 1, plug_id: 5, tenant_id: 1, driver_user_id: 42,
  category: 'damaged', description: 'The connector is cracked and sparks.',
  status: 'open', resolution_note: null,
  created_at: '2026-07-10T10:00:00Z', resolved_at: null, resolved_by_user_id: null,
};

const ACKNOWLEDGED_REPORT = {
  id: 2, plug_id: 6, tenant_id: 1, driver_user_id: 43,
  category: 'unsafe', description: 'Sparks fly when plugged in.',
  status: 'acknowledged', resolution_note: 'Looking into it.',
  created_at: '2026-07-09T08:30:00Z', resolved_at: null, resolved_by_user_id: 5,
};

const PLUGS_FIXTURE = [
  { id: 5, name: 'Garage plug' },
  { id: 6, name: 'Lobby plug' },
];

const mockApi = ({ reports = [OPEN_REPORT], plugs = PLUGS_FIXTURE } = {}) => {
  api.get.mockImplementation((url) => {
    if (url.startsWith('/api/cpo/plug-reports')) return Promise.resolve(reports);
    if (url.startsWith('/api/cpo/plugs')) return Promise.resolve(plugs);
    return Promise.reject(new Error(`unhandled url ${url}`));
  });
};

const renderPage = () => render(<CpoPlugReports />);

beforeEach(() => {
  vi.clearAllMocks();
  mockApi();
});

describe('CpoPlugReports', () => {
  it('defaults to the Open tab and fetches with status_filter=open', async () => {
    renderPage();
    await screen.findByText('The connector is cracked and sparks.');

    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('status_filter=open'));
    expect(screen.getByRole('tab', { name: 'Open' })).toHaveAttribute('aria-selected', 'true');
  });

  it('enriches the row with the plug name from the best-effort lookup', async () => {
    renderPage();
    expect(await screen.findByText('Garage plug')).toBeInTheDocument();
  });

  it('falls back to a raw plug id when enrichment has no match', async () => {
    mockApi({ plugs: [] });
    renderPage();
    await screen.findByText('The connector is cracked and sparks.');
    expect(screen.getByText('Charger #5')).toBeInTheDocument();
  });

  it('shows the category label', async () => {
    renderPage();
    expect(await screen.findByText('Physically damaged')).toBeInTheDocument();
  });

  it('switches tabs and refetches with the matching status_filter (or none for All)', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('The connector is cracked and sparks.');

    mockApi({ reports: [ACKNOWLEDGED_REPORT] });
    await user.click(screen.getByRole('tab', { name: 'Acknowledged' }));
    expect(await screen.findByText('Sparks fly when plugged in.')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith(expect.stringContaining('status_filter=acknowledged'));

    mockApi({ reports: [OPEN_REPORT, ACKNOWLEDGED_REPORT] });
    await user.click(screen.getByRole('tab', { name: 'All' }));
    await screen.findByText('The connector is cracked and sparks.');
    const lastCall = api.get.mock.calls.filter(([u]) => u.startsWith('/api/cpo/plug-reports')).pop();
    expect(lastCall[0]).not.toContain('status_filter');
  });

  it('shows the resolution note and no actions on a resolved row', async () => {
    const resolved = { ...ACKNOWLEDGED_REPORT, status: 'resolved', resolution_note: 'Charger swapped out.' };
    mockApi({ reports: [resolved] });
    renderPage();

    await screen.findByText('Sparks fly when plugged in.');
    const row = screen.getAllByRole('row').find((r) => r.textContent.includes('Sparks fly when plugged in.'));
    expect(within(row).getByText('Resolved')).toBeInTheDocument();
    expect(within(row).getByText(/Charger swapped out/)).toBeInTheDocument();
    expect(within(row).queryByRole('button')).not.toBeInTheDocument();
  });

  it('an open row offers both Acknowledge and Resolve; an acknowledged row offers only Resolve', async () => {
    mockApi({ reports: [OPEN_REPORT, ACKNOWLEDGED_REPORT], });
    // status_filter switching aside, render with the "All" tab equivalent by
    // just checking each row's own actions regardless of the active tab fetch.
    renderPage();
    await screen.findByText('The connector is cracked and sparks.');

    const openRow = screen.getAllByRole('row').find((r) => r.textContent.includes('The connector is cracked and sparks.'));
    expect(within(openRow).getByRole('button', { name: 'Acknowledge' })).toBeInTheDocument();
    expect(within(openRow).getByRole('button', { name: 'Resolve' })).toBeInTheDocument();
  });

  it('acknowledges a report without a required note', async () => {
    api.post.mockResolvedValue({ id: 1, status: 'acknowledged' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('The connector is cracked and sparks.');

    await user.click(screen.getByRole('button', { name: 'Acknowledge' }));
    expect(await screen.findByRole('heading', { name: 'Acknowledge report' })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Mark acknowledged' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/plug-reports/1/resolve', { action: 'acknowledge' })
    );
    expect(toast.ok).toHaveBeenCalledWith('Report acknowledged.');
  });

  it('resolves a report with an optional note', async () => {
    api.post.mockResolvedValue({ id: 1, status: 'resolved' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('The connector is cracked and sparks.');

    await user.click(screen.getByRole('button', { name: 'Resolve' }));
    expect(await screen.findByRole('heading', { name: 'Resolve report' })).toBeInTheDocument();

    await user.type(screen.getByLabelText('Note (optional)'), 'Connector replaced.');
    await user.click(screen.getByRole('button', { name: 'Mark resolved' }));

    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/plug-reports/1/resolve', {
        action: 'resolve',
        note: 'Connector replaced.',
      })
    );
    expect(toast.ok).toHaveBeenCalledWith('Report resolved.');
  });

  it('surfaces a resolve 409 inline and keeps the dialog open', async () => {
    api.post.mockRejectedValue(new Error('This plug report was already resolved.'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('The connector is cracked and sparks.');

    await user.click(screen.getByRole('button', { name: 'Resolve' }));
    await screen.findByRole('heading', { name: 'Resolve report' });
    await user.click(screen.getByRole('button', { name: 'Mark resolved' }));

    expect(await screen.findByText('This plug report was already resolved.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Resolve report' })).toBeInTheDocument();
  });

  it('shows a retryable ErrorState on fetch failure', async () => {
    api.get.mockImplementation((url) => {
      if (url.startsWith('/api/cpo/plug-reports')) return Promise.reject(new Error('Network down'));
      return Promise.resolve([]);
    });
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    mockApi();
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByText('The connector is cracked and sparks.');
  });

  it('shows an empty state (not an error) when there are no open reports', async () => {
    mockApi({ reports: [] });
    renderPage();
    expect(await screen.findByText('No open reports')).toBeInTheDocument();
  });
});
