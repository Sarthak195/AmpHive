/**
 * GatewayChecklist: the read-only commissioning checklist for one CPO gateway
 * (GET /api/cpo/gateways/{id}/selftest), shown at the top of the gateway
 * diagnostics drawer. Loads on open; "Re-check" fetches again. Nothing here
 * touches the hardware: the server computes it from rows it already holds. The
 * Wi-Fi line is labelled by the server: "(live)" for the signal firmware
 * 2.10.0 or newer reports every 5 minutes, "(at boot)" for the boot report.
 */

import { useCallback, useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import api from '../api/client';
import { ErrorState, Skeleton } from './ui';
import './GatewayChecklist.css';

const BADGE = { pass: 'badge-ok', warn: 'badge-warn', fail: 'badge-danger', unknown: 'badge-info' };
const LABEL = { pass: 'Pass', warn: 'Warning', fail: 'Fail', unknown: 'Unknown' };

export default function GatewayChecklist({ gatewayId }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const run = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.get(`/api/cpo/gateways/${gatewayId}/selftest`));
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [gatewayId]);

  useEffect(() => {
    setData(null);
    run();
  }, [run]);

  return (
    <section className="gwcheck" aria-label="Commissioning checklist">
      <div className="gwcheck-head">
        <h3 className="gwcheck-title">Commissioning checklist</h3>
        {data && !loading && (
          <span className={`badge ${BADGE[data.overall] || 'badge-info'}`}>{LABEL[data.overall]}</span>
        )}
        <button type="button" className="btn btn-quiet btn-sm" onClick={run} disabled={loading}>
          <RefreshCw size={15} aria-hidden="true" />
          Re-check
        </button>
      </div>
      {error && <ErrorState error={error} onRetry={run} />}
      {!error && !data && loading && <Skeleton />}
      {data && (
        <ul className="gwcheck-list" aria-busy={loading}>
          {data.checks.map((c) => (
            <li key={c.key} className="gwcheck-item">
              <span className={`badge ${BADGE[c.status] || 'badge-info'}`}>{LABEL[c.status]}</span>
              <div>
                <div>{c.label}</div>
                <div className="text-2 text-sm">{c.detail}</div>
                {c.fix_hint && <div className="text-sm">Fix: {c.fix_hint}</div>}
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
