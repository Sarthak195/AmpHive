/**
 * GatewayLogsPanel: forwarded firmware log lines (TD#28), shared by the CPO
 * gateway drawer and the admin Logs tab.
 *
 * `path` is the list endpoint (GET .../logs for one CPO gateway, or
 * /api/admin/gateway-logs); `params` adds fixed query params (e.g.
 * gateway_id for admin). Both endpoints return { total, items } newest first
 * and accept ONE `level` (error | warning | info), so the default "Warnings
 * and errors" view issues one request per level and merges them by time.
 * Paging keeps a per-level offset; auto-refresh (opt-in, 15 s, paused while
 * the tab is hidden) re-fetches the first page only, and is skipped while a
 * Load more is in flight or once more than one page is loaded (a refresh would
 * collapse the list back to page one). Merged pages are de-duplicated by id.
 *
 * Firmware forwards only WARN/ERROR lines (log_forward_vprintf), so the
 * Info filter mostly holds malformed or non-firmware lines.
 *
 * Log text is untrusted device output: it is rendered as React text nodes
 * only (never HTML), with ANSI colour escapes stripped for display.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import api from '../api/client';
import { PAGE_SIZE, REFRESH_MS, RETENTION_DAYS, cleanLogLine } from '../utils/gatewayLogs';
import { EmptyState, ErrorState, Skeleton } from './ui';
import './GatewayLogsPanel.css';

const LEVEL_SETS = {
  'warn-error': { label: 'Warnings and errors', levels: ['error', 'warning'] },
  error: { label: 'Errors only', levels: ['error'] },
  warning: { label: 'Warnings only', levels: ['warning'] },
  info: { label: 'Info', levels: ['info'] },
  all: { label: 'All levels', levels: [null] },
};

const BADGE = { error: 'badge-danger', warning: 'badge-warn', info: 'badge-info' };

const byNewest = (a, b) => {
  const ta = a.created_at ? new Date(a.created_at).getTime() : 0;
  const tb = b.created_at ? new Date(b.created_at).getTime() : 0;
  return tb - ta || b.id - a.id;
};

const fmtTime = (iso) =>
  iso
    ? new Date(iso).toLocaleString('en-IN', {
        month: 'short',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })
    : '';

export default function GatewayLogsPanel({ path, params = {}, showGateway = false }) {
  const [levelKey, setLevelKey] = useState('warn-error');
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [total, setTotal] = useState(0);

  // Per-level paging state lives in refs (no renders needed); `gen` discards
  // responses that belong to a superseded filter or refresh.
  const offsetsRef = useRef({});
  const totalsRef = useRef({});
  const gen = useRef(0);
  const loadingMoreRef = useRef(false);
  const pagedRef = useRef(false);
  const paramsKey = JSON.stringify(params);

  const fetchLevel = useCallback(
    async (level, offset) => {
      const qs = new URLSearchParams({
        ...JSON.parse(paramsKey),
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (level) qs.set('level', level);
      const res = await api.get(`${path}?${qs.toString()}`);
      return { items: res?.items || [], total: res?.total || 0 };
    },
    [path, paramsKey],
  );

  const load = useCallback(
    async ({ more = false, silent = false } = {}) => {
      const levels = LEVEL_SETS[levelKey].levels;
      const myGen = more ? gen.current : ++gen.current;
      if (more) {
        loadingMoreRef.current = true;
        setLoadingMore(true);
      }
      else if (!silent) setLoading(true);
      try {
        const targets = levels.filter((lv) => {
          if (!more) return true;
          const k = lv || 'all';
          return (offsetsRef.current[k] || 0) < (totalsRef.current[k] || 0);
        });
        const pages = await Promise.all(
          targets.map((lv) => fetchLevel(lv, more ? offsetsRef.current[lv || 'all'] || 0 : 0)),
        );
        if (myGen !== gen.current) return;
        if (!more) {
          pagedRef.current = false;
          offsetsRef.current = {};
          totalsRef.current = {};
        }
        const fresh = [];
        targets.forEach((lv, i) => {
          const k = lv || 'all';
          offsetsRef.current[k] = (more ? offsetsRef.current[k] || 0 : 0) + pages[i].items.length;
          totalsRef.current[k] = pages[i].total;
          fresh.push(...pages[i].items);
        });
        if (more) pagedRef.current = true;
        setItems((prev) => {
          const merged = more ? [...prev, ...fresh] : fresh;
          return [...new Map(merged.map((r) => [r.id, r])).values()].sort(byNewest);
        });
        const keys = levels.map((lv) => lv || 'all');
        setTotal(keys.reduce((s, k) => s + (totalsRef.current[k] || 0), 0));
        setHasMore(keys.some((k) => (offsetsRef.current[k] || 0) < (totalsRef.current[k] || 0)));
        setError(null);
      } catch (err) {
        if (myGen === gen.current && !silent) setError(err);
      } finally {
        if (more) loadingMoreRef.current = false;
        if (myGen === gen.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [levelKey, fetchLevel],
  );

  useEffect(() => {
    load();
  }, [load]);

  // Opt-in auto-refresh: first page only, never while the tab is hidden.
  useEffect(() => {
    if (!autoRefresh) return undefined;
    const tick = () => {
      if (!document.hidden && !loadingMoreRef.current && !pagedRef.current) {
        load({ silent: true });
      }
    };
    const id = setInterval(tick, REFRESH_MS);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(id);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [autoRefresh, load]);

  return (
    <div className="stack">
      <div className="gwlogs-controls">
        <label htmlFor="gwlogs-level" className="sr-only">
          Log level
        </label>
        <select
          id="gwlogs-level"
          className="select"
          value={levelKey}
          onChange={(e) => setLevelKey(e.target.value)}
        >
          {Object.entries(LEVEL_SETS).map(([key, { label }]) => (
            <option key={key} value={key}>
              {label}
            </option>
          ))}
        </select>
        <label className="row text-sm">
          <input
            type="checkbox"
            checked={autoRefresh}
            onChange={(e) => setAutoRefresh(e.target.checked)}
          />
          Auto-refresh (15 s)
        </label>
        <button type="button" className="btn btn-quiet btn-sm" onClick={() => load()} disabled={loading}>
          Refresh
        </button>
        <span className="filter-count" aria-live="polite">
          {total} line{total === 1 ? '' : 's'}
        </span>
      </div>

      {loading ? (
        <Skeleton lines={4} />
      ) : error ? (
        <ErrorState error={error} onRetry={() => load()} title="Couldn't load logs" />
      ) : items.length === 0 ? (
        <EmptyState
          title="No log lines"
          body={`Nothing matches this filter. Firmware 2.1.0 and later forwards WARN and ERROR lines, and logs are kept for a limited time, ${RETENTION_DAYS} days by default.`}
        />
      ) : (
        <>
          <ul className="gwlogs-list" aria-label="Gateway log lines">
            {items.map((row) => (
              <li key={row.id} className="gwlogs-item">
                <div className="gwlogs-meta">
                  <span className={`badge ${BADGE[row.level] || 'badge-info'}`}>{row.level}</span>
                  <time className="text-2" dateTime={row.created_at}>
                    {fmtTime(row.created_at)}
                  </time>
                </div>
                <div>
                  {showGateway && (
                    <span className="text-2 text-sm">
                      {row.tenant_name ? `${row.tenant_name} · ` : ''}
                      {row.gateway_id}
                    </span>
                  )}
                  <pre className="gwlogs-msg">{cleanLogLine(row.message)}</pre>
                </div>
              </li>
            ))}
          </ul>
          {hasMore && (
            <button
              type="button"
              className="btn btn-quiet btn-full"
              onClick={() => load({ more: true })}
              disabled={loadingMore}
            >
              {loadingMore ? 'Loading…' : 'Load more'}
            </button>
          )}
        </>
      )}
    </div>
  );
}
