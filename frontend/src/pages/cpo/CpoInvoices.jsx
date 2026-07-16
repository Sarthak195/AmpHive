/**
 * AmpHive CPO Invoices Page
 * =========================
 * Operators review the GST invoices issued for completed sessions. An invoice is
 * generated the first time a driver views it for a billed session, so this list
 * is read-only: number, date, session, energy, and the GST breakdown.
 *
 * Data: GET /api/cpo/invoices (list) and GET /api/cpo/profile (tenant name).
 * The printable copy lives at GET /api/sessions/{id}/invoice?format=html, which
 * returns rendered HTML (not JSON) and needs the Bearer token — so "View" uses a
 * raw fetch + blob URL rather than the JSON api client, opening it in a new tab.
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';

const CpoInvoices = () => {
  const [invoices, setInvoices] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const fetchInvoices = useCallback(async () => {
    setLoading(true);
    try {
      const [invoiceRes, profileRes] = await Promise.all([
        api.get('/api/cpo/invoices'),
        api.get('/api/cpo/profile'),
      ]);
      setInvoices(invoiceRes);
      setTenantName(profileRes.tenant?.name || '');
    } catch (err) {
      console.error('Failed to load invoices:', err);
      setError(err.message || 'Failed to load invoices.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { fetchInvoices(); }, [fetchInvoices]);

  // The HTML invoice endpoint returns rendered markup (not JSON) and requires the
  // Bearer token, so we fetch it directly and open the blob in a new tab.
  const viewInvoice = async (sessionId) => {
    setError('');
    try {
      const base = import.meta.env.VITE_API_URL || '';
      const token = localStorage.getItem('amphive_token');
      const res = await fetch(`${base}/api/sessions/${sessionId}/invoice?format=html`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      window.open(url, '_blank');
    } catch (err) {
      setError(err.message || 'Failed to open the invoice.');
    }
  };

  return (
    <CpoLayout tenantName={tenantName}>
      <div className="cpo-page-header">
        <h1>Invoices</h1>
        <p>Issued GST invoices for completed sessions</p>
      </div>

      {error && (
        <div className="glass" style={{
          padding: '0.75rem 1rem', marginBottom: '1rem',
          borderRadius: 'var(--radius-md)',
          border: '1px solid var(--color-danger)',
          color: 'var(--color-danger)', fontSize: '0.85rem',
        }}>
          {error}
        </div>
      )}

      {loading ? (
        <div className="data-table-container">
          {[1, 2, 3].map((i) => (
            <div key={i} style={{ padding: '1rem', borderBottom: '1px solid hsla(0,0%,100%,0.04)' }}>
              <div className="skeleton" style={{ width: '100%', height: '18px' }} />
            </div>
          ))}
        </div>
      ) : invoices.length > 0 ? (
        <div className="data-table-container animate-fade-in">
          <table className="data-table">
            <thead>
              <tr>
                <th>Invoice #</th>
                <th>Date</th>
                <th>Session</th>
                <th>Energy</th>
                <th>Total</th>
                <th>GST</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {invoices.map((inv) => (
                <tr key={inv.id}>
                  <td className="mono">{inv.invoice_number}</td>
                  <td>{new Date(inv.issued_at).toLocaleDateString()}</td>
                  <td>#{inv.session_id}</td>
                  <td>{inv.line_item?.energy_kwh} kWh</td>
                  <td className="num">{inv.total_inr}</td>
                  <td className="num">{inv.gst_amount_inr}</td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={() => viewInvoice(inv.session_id)}>
                      View
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="empty-state glass">
          <div className="empty-state-icon">🧾</div>
          <h3>No invoices yet</h3>
          <p>Invoices are generated when a driver views one for a completed session.</p>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoInvoices;
