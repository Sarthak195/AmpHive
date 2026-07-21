/**
 * CpoInvoices (redesign v3, D7) — the tenant's issued GST invoices.
 *
 * An invoice is generated the first time a driver (or this console) views
 * one for a completed, billed session — this list is read-only: number,
 * date, session, energy, and the ₹ total. GST invoices are intra-state
 * (CGST+SGST) HTML only, per the redesign contract.
 *
 * Data: GET /api/cpo/invoices?limit&offset -> {total, items} (paginated).
 * Export: GET /api/cpo/invoices.csv[?days=] — a file download, so it's a raw
 * authenticated fetch, not the JSON api client (mirrors CpoSessions' CSV
 * export). `days` only narrows the CSV: the JSON list endpoint doesn't take
 * a days filter (it's a legal ledger — newest first, paginated, full
 * history), so the on-screen table stays unfiltered by date and the
 * selector applies only to what gets exported.
 * "View" opens the printable copy at GET /api/sessions/{id}/invoice?format=html
 * — rendered HTML (not JSON), fetched raw with the bearer token and opened
 * as a blob in a new tab.
 */

import { useCallback, useEffect, useState } from 'react';
import { Download, FileText } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, useToast } from '../../components/ui';
import api from '../../api/client';
import { useConfig } from '../../contexts/ConfigContext';
import { formatKwh } from '../../utils/money';
import { apiErrorCopy } from '../../utils/statusCopy';

const LIMIT = 20;
const EXPORT_DAYS_OPTIONS = [
  { value: '', label: 'Full history' },
  { value: '30', label: 'Last 30 days' },
  { value: '90', label: 'Last 90 days' },
  { value: '365', label: 'Last 12 months' },
];

const formatDate = (iso) =>
  iso ? new Date(iso).toLocaleDateString('en-IN', { year: 'numeric', month: 'short', day: 'numeric' }) : '—';

export default function CpoInvoices() {
  const { currency = 'INR' } = useConfig();
  const toast = useToast();

  const [invoices, setInvoices] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchInvoices = useCallback(async (nextOffset = 0) => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get(`/api/cpo/invoices?limit=${LIMIT}&offset=${nextOffset}`);
      if (Array.isArray(res)) {
        setInvoices(res);
        setTotal(res.length);
      } else {
        setInvoices(res?.items || []);
        setTotal(typeof res?.total === 'number' ? res.total : (res?.items || []).length);
      }
      setOffset(nextOffset);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchInvoices(0);
  }, [fetchInvoices]);

  // ---- Export CSV ---------------------------------------------------------
  const [exportDays, setExportDays] = useState('');
  const [exporting, setExporting] = useState(false);

  const exportCsv = async () => {
    setExporting(true);
    try {
      const params = new URLSearchParams();
      if (exportDays) params.set('days', exportDays);
      const base = import.meta.env.VITE_API_URL || '';
      const token = localStorage.getItem('amphive_token');
      const qs = params.toString();
      const res = await fetch(`${base}/api/cpo/invoices.csv${qs ? `?${qs}` : ''}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Export failed (${res.status})`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `amphive-invoices-${new Date().toISOString().slice(0, 10)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      toast.ok('Invoices exported.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setExporting(false);
    }
  };

  // ---- View printable HTML copy -------------------------------------------
  const [viewingId, setViewingId] = useState(null);

  const viewInvoice = async (sessionId) => {
    setViewingId(sessionId);
    try {
      const base = import.meta.env.VITE_API_URL || '';
      const token = localStorage.getItem('amphive_token');
      const res = await fetch(`${base}/api/sessions/${sessionId}/invoice?format=html`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (!res.ok) throw new Error(`Couldn't open the invoice (${res.status}).`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
      // Give the new tab time to load the blob before reclaiming it.
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setViewingId(null);
    }
  };

  const columns = [
    { key: 'invoice_number', label: 'Invoice #', render: (inv) => <span className="mono">{inv.invoice_number}</span> },
    { key: 'issued_at', label: 'Date', render: (inv) => formatDate(inv.issued_at) },
    { key: 'session_id', label: 'Session', render: (inv) => `#${inv.session_id}` },
    { key: 'energy_kwh', label: 'Energy', num: true, render: (inv) => formatKwh(inv.line_item?.energy_kwh) },
    { key: 'total_inr', label: 'Total', num: true, render: (inv) => `₹${Number(inv.total_inr ?? 0).toFixed(2)}` },
    { key: 'gst_amount_inr', label: 'GST', num: true, render: (inv) => `₹${Number(inv.gst_amount_inr ?? 0).toFixed(2)}` },
    {
      key: 'actions',
      label: '',
      render: (inv) => (
        <button
          type="button"
          className="btn btn-quiet btn-sm"
          disabled={viewingId === inv.session_id}
          onClick={() => viewInvoice(inv.session_id)}
        >
          {viewingId === inv.session_id ? 'Opening…' : 'View'}
        </button>
      ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Invoices"
        sub={`Issued GST invoices for completed sessions (${currency}).`}
        actions={
          <div className="row">
            <select
              className="select"
              aria-label="Export period"
              value={exportDays}
              onChange={(e) => setExportDays(e.target.value)}
            >
              {EXPORT_DAYS_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
            <button
              type="button"
              className="btn btn-quiet"
              onClick={exportCsv}
              disabled={exporting || invoices.length === 0}
            >
              <Download size={16} aria-hidden="true" />
              {exporting ? 'Exporting…' : 'Export CSV'}
            </button>
          </div>
        }
      />

      <DataTable
        columns={columns}
        rows={invoices}
        loading={loading}
        error={error}
        onRetry={() => fetchInvoices(offset)}
        emptyIcon={FileText}
        emptyTitle="No invoices yet"
        emptyBody="Invoices are generated when a driver (or you) view one for a completed, billed session."
        pagination={{ total, offset, limit: LIMIT, onPage: (o) => fetchInvoices(o) }}
        collapse
      />
    </CpoLayout>
  );
}
