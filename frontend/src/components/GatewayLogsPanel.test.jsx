/**
 * GatewayLogsPanel tests: the default "warnings and errors" view merges one
 * request per level, paging uses per-level offsets, firmware text is rendered
 * as plain text (ANSI stripped, HTML never parsed), the empty state names the
 * retention, and opt-in auto-refresh skips ticks while the tab is hidden.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import GatewayLogsPanel from './GatewayLogsPanel';
import { PAGE_SIZE, REFRESH_MS, cleanLogLine } from '../utils/gatewayLogs';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const PATH = '/api/cpo/gateways/gw1/logs';
const row = (id, level, message, created_at) => ({ id, gateway_id: 'gw1', level, message, created_at });

const ESC = String.fromCharCode(27);
const ERR = row(3, 'error', `${ESC}[0;31mE (100) mqtt: connect failed${ESC}[0m`, '2026-11-17T10:03:00Z');
const WARN = row(2, 'warning', 'W (90) wifi: <img src=x onerror=alert(1)>', '2026-11-17T10:02:00Z');

const urlsCalled = () => api.get.mock.calls.map((c) => c[0]);

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockImplementation((url) => {
    if (url.includes('level=error')) return Promise.resolve({ total: 1, items: [ERR] });
    if (url.includes('level=warning')) return Promise.resolve({ total: 1, items: [WARN] });
    return Promise.resolve({ total: 0, items: [] });
  });
});

describe('GatewayLogsPanel', () => {
  it('cleanLogLine strips ANSI colour escapes only', () => {
    expect(cleanLogLine(`${ESC}[0;33mW (1) x: y${ESC}[0m`)).toBe('W (1) x: y');
  });

  it('defaults to warning+error: one request per level, merged newest first', async () => {
    render(<GatewayLogsPanel path={PATH} />);

    expect(await screen.findByText('E (100) mqtt: connect failed')).toBeInTheDocument();
    const urls = urlsCalled();
    expect(urls).toHaveLength(2);
    expect(urls.some((u) => u.includes('level=error'))).toBe(true);
    expect(urls.some((u) => u.includes('level=warning'))).toBe(true);
    expect(urls.every((u) => !u.includes('level=info'))).toBe(true);

    const items = screen.getAllByRole('listitem');
    expect(items[0]).toHaveTextContent('connect failed');
    expect(items[1]).toHaveTextContent('wifi');
  });

  it('renders device text as plain text, never as HTML', async () => {
    const { container } = render(<GatewayLogsPanel path={PATH} />);
    await screen.findByText(/<img src=x onerror=alert\(1\)>/);
    expect(container.querySelector('img')).toBeNull();
  });

  it('info filter requests level=info only', async () => {
    const user = userEvent.setup();
    render(<GatewayLogsPanel path={PATH} />);
    await screen.findByText('E (100) mqtt: connect failed');
    api.get.mockClear();

    await user.selectOptions(screen.getByLabelText('Log level'), 'info');
    await screen.findByText('No log lines');
    expect(urlsCalled()).toHaveLength(1);
    expect(urlsCalled()[0]).toContain('level=info');
  });

  it('empty state names the 14-day retention', async () => {
    api.get.mockResolvedValue({ total: 0, items: [] });
    render(<GatewayLogsPanel path={PATH} />);
    expect(await screen.findByText(/kept for a limited time, 14 days by default/)).toBeInTheDocument();
  });

  it('Load more fetches the next page at the per-level offset and appends', async () => {
    const page = Array.from({ length: PAGE_SIZE }, (_, i) =>
      row(1000 - i, 'error', `E (${i}) tag: line ${i}`, `2026-11-17T09:${String(59 - (i % 60)).padStart(2, '0')}:00Z`),
    );
    api.get.mockImplementation((url) => {
      if (url.includes('level=error')) {
        return Promise.resolve(
          url.includes('offset=0')
            ? { total: PAGE_SIZE + 1, items: page }
            : { total: PAGE_SIZE + 1, items: [row(1, 'error', 'E (9) old: oldest line', '2026-11-16T00:00:00Z')] },
        );
      }
      return Promise.resolve({ total: 0, items: [] });
    });
    const user = userEvent.setup();
    render(<GatewayLogsPanel path={PATH} />);

    await user.click(await screen.findByRole('button', { name: 'Load more' }));
    expect(await screen.findByText('E (9) old: oldest line')).toBeInTheDocument();
    expect(urlsCalled().some((u) => u.includes('level=error') && u.includes(`offset=${PAGE_SIZE}`))).toBe(true);
    expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull();
  });

  it('de-duplicates rows that appear on two merged pages', async () => {
    const page = Array.from({ length: PAGE_SIZE }, (_, i) =>
      row(1000 - i, 'error', `E (${i}) tag: line ${i}`, `2026-11-17T09:${String(59 - (i % 60)).padStart(2, '0')}:00Z`),
    );
    api.get.mockImplementation((url) => {
      if (!url.includes('level=error')) return Promise.resolve({ total: 0, items: [] });
      return Promise.resolve(
        url.includes('offset=0')
          ? { total: PAGE_SIZE + 2, items: page }
          // The window shifted: the last row of page 1 comes back again.
          : { total: PAGE_SIZE + 2, items: [page[PAGE_SIZE - 1], row(1, 'error', 'E (9) old: oldest line', '2026-11-16T00:00:00Z')] },
      );
    });
    const user = userEvent.setup();
    render(<GatewayLogsPanel path={PATH} />);

    await user.click(await screen.findByRole('button', { name: 'Load more' }));
    await screen.findByText('E (9) old: oldest line');
    expect(screen.getAllByRole('listitem')).toHaveLength(PAGE_SIZE + 1);
    expect(screen.getAllByText(`E (${PAGE_SIZE - 1}) tag: line ${PAGE_SIZE - 1}`)).toHaveLength(1);
  });

  describe('auto-refresh', () => {
    const setHidden = (hidden) =>
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });

    beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
    afterEach(() => {
      setHidden(false);
      vi.useRealTimers();
    });

    it('polls every 15 s while visible and skips ticks while the tab is hidden', async () => {
      render(<GatewayLogsPanel path={PATH} />);
      await screen.findByText('E (100) mqtt: connect failed');
      await act(async () => {
        screen.getByLabelText(/Auto-refresh/).click();
      });
      api.get.mockClear();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(REFRESH_MS);
      });
      expect(api.get).toHaveBeenCalledTimes(2);

      api.get.mockClear();
      setHidden(true);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(REFRESH_MS * 2);
      });
      expect(api.get).not.toHaveBeenCalled();
    });

    it('stops refreshing once a second page is loaded (it would collapse the list)', async () => {
      const page = Array.from({ length: PAGE_SIZE }, (_, i) =>
        row(1000 - i, 'error', `E (${i}) tag: line ${i}`, '2026-11-17T09:00:00Z'),
      );
      api.get.mockImplementation((url) => {
        if (!url.includes('level=error')) return Promise.resolve({ total: 0, items: [] });
        return Promise.resolve(
          url.includes('offset=0')
            ? { total: PAGE_SIZE + 1, items: page }
            : { total: PAGE_SIZE + 1, items: [row(1, 'error', 'E (9) old: oldest line', '2026-11-16T00:00:00Z')] },
        );
      });
      render(<GatewayLogsPanel path={PATH} />);
      const more = await screen.findByRole('button', { name: 'Load more' });
      await act(async () => {
        more.click();
      });
      await screen.findByText('E (9) old: oldest line');
      await act(async () => {
        screen.getByLabelText(/Auto-refresh/).click();
      });
      api.get.mockClear();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(REFRESH_MS * 2);
      });
      expect(api.get).not.toHaveBeenCalled();
      expect(screen.getByText('E (9) old: oldest line')).toBeInTheDocument();
    });
  });
});
