/**
 * AmpHive CPO Settings Page (GST invoicing)
 * =========================================
 * Operators configure the tax details that appear on the GST invoices drivers
 * download for their charging sessions: the tenant's GSTIN, the legal name as
 * printed on the GST certificate, and a short prefix for invoice numbers
 * (e.g. "ACME-0001"). All three are optional — clearing a field is allowed and
 * simply omits it from generated invoices.
 *
 * Data: GET /api/cpo/profile -> { tenant: { name, gstin, legal_name, invoice_prefix, ... } }
 *       PUT /api/cpo/profile { gstin, legal_name, invoice_prefix }.
 */

import { useState, useEffect, useCallback } from 'react';
import CpoLayout from '../../components/CpoLayout';
import api from '../../api/client';

const CpoSettings = () => {
  const [tenantName, setTenantName] = useState('');
  const [gstin, setGstin] = useState('');
  const [legalName, setLegalName] = useState('');
  const [invoicePrefix, setInvoicePrefix] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);

  const fetchProfile = useCallback(async () => {
    try {
      const res = await api.get('/api/cpo/profile');
      const t = res.tenant || {};
      setTenantName(t.name || '');
      setGstin(t.gstin || '');
      setLegalName(t.legal_name || '');
      setInvoicePrefix(t.invoice_prefix || '');
    } catch (err) {
      setError(err.message || 'Failed to load settings.');
    }
  }, []);

  useEffect(() => { fetchProfile(); }, [fetchProfile]);

  const save = async (e) => {
    e.preventDefault();
    setError('');
    setSaved(false);
    setSaving(true);
    try {
      await api.put('/api/cpo/profile', {
        gstin: gstin.trim(),
        legal_name: legalName.trim(),
        invoice_prefix: invoicePrefix.trim(),
      });
      setSaved(true);
      await fetchProfile();
    } catch (err) {
      setError(err.message || 'Failed to save settings.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <CpoLayout tenantName={tenantName}>
      <div className="cpo-page-header">
        <h1>Settings</h1>
        <p>GST invoicing details for your charging network</p>
      </div>

      <p style={{ color: 'var(--color-text-secondary)', fontSize: '0.88rem', maxWidth: '42rem', marginBottom: '1rem' }}>
        These details appear on the GST tax invoices drivers can download for their
        charging sessions. Leave a field blank to omit it from generated invoices.
      </p>

      <form className="filter-bar" onSubmit={save} style={{ flexWrap: 'wrap', alignItems: 'flex-end' }}>
        <label style={{ fontSize: '0.82rem', color: 'var(--color-text-secondary)', display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
          GSTIN
          <input
            type="text"
            maxLength={15}
            placeholder="22AAAAA0000A1Z5"
            value={gstin}
            onChange={(e) => setGstin(e.target.value)}
          />
        </label>
        <label style={{ fontSize: '0.82rem', color: 'var(--color-text-secondary)', display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
          Legal name (as on the GST certificate)
          <input
            type="text"
            maxLength={120}
            placeholder="Acme Charging Pvt Ltd"
            value={legalName}
            onChange={(e) => setLegalName(e.target.value)}
          />
        </label>
        <label style={{ fontSize: '0.82rem', color: 'var(--color-text-secondary)', display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
          Invoice number prefix
          <input
            type="text"
            maxLength={12}
            placeholder="ACME"
            value={invoicePrefix}
            onChange={(e) => setInvoicePrefix(e.target.value)}
          />
        </label>
        <button className="btn btn-primary btn-sm" type="submit" disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </form>

      {saved && (
        <div style={{ color: 'var(--color-text-secondary)', fontSize: '0.85rem', marginBottom: '1rem' }}>
          Saved.
        </div>
      )}

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
    </CpoLayout>
  );
};

export default CpoSettings;
