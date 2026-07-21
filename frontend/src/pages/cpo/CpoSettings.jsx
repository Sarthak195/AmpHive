/**
 * CpoSettings (redesign v3, D8) — three independently-saved cards for the
 * operator's organization identity, queued-charge defaults and GST
 * invoicing.
 *
 * Data: GET /api/cpo/profile -> { tenant: { name, timezone,
 *       queued_charging_enabled, auto_start_delay_min, queue_ttl_min,
 *       gstin, legal_name, invoice_prefix } } (backend/routers/cpo.py
 *       cpo_profile). The tenant name and timezone aren't writable through
 *       this endpoint — CpoProfileUpdateRequest (backend/schemas.py) has no
 *       `name`/`timezone` field — so both render read-only.
 *
 * PUT /api/cpo/profile accepts, all optional and independently settable:
 *   { queued_charging_enabled, auto_start_delay_min, queue_ttl_min,
 *     gstin, legal_name, invoice_prefix }
 * — the Defaults card sends only the first three, the GST card only the
 * last three, so saving one card never clobbers the other's fields.
 */

import { useCallback, useEffect, useState } from 'react';
import { Building2, Receipt, Zap } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, Skeleton, SkeletonTitle, ErrorState, useToast } from '../../components/ui';
import api from '../../api/client';
import { apiErrorCopy } from '../../utils/statusCopy';
import './CpoSettings.css';

// Soft check only — informational, never blocks a save.
const GSTIN_RE = /^[0-9]{2}[A-Z0-9]{10}[0-9A-Z]{3}$/;

const CpoSettings = () => {
  const toast = useToast();

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [tenant, setTenant] = useState(null);

  // Defaults card
  const [queuedEnabled, setQueuedEnabled] = useState(false);
  const [autoStartDelay, setAutoStartDelay] = useState('2');
  const [queueTtl, setQueueTtl] = useState('720');
  const [defaultsError, setDefaultsError] = useState('');
  const [defaultsBusy, setDefaultsBusy] = useState(false);

  // GST & invoicing card
  const [gstin, setGstin] = useState('');
  const [legalName, setLegalName] = useState('');
  const [invoicePrefix, setInvoicePrefix] = useState('');
  const [gstBusy, setGstBusy] = useState(false);

  const applyTenant = (t) => {
    setQueuedEnabled(Boolean(t.queued_charging_enabled));
    setAutoStartDelay(String(t.auto_start_delay_min ?? 2));
    setQueueTtl(String(t.queue_ttl_min ?? 720));
    setGstin(t.gstin || '');
    setLegalName(t.legal_name || '');
    setInvoicePrefix(t.invoice_prefix || '');
  };

  const fetchProfile = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get('/api/cpo/profile');
      const t = res?.tenant || {};
      setTenant(t);
      applyTenant(t);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchProfile();
  }, [fetchProfile]);

  const saveDefaults = async (e) => {
    e.preventDefault();
    setDefaultsError('');

    const delay = Number(autoStartDelay);
    const ttl = Number(queueTtl);
    if (!Number.isInteger(delay) || delay < 0 || delay > 1440) {
      setDefaultsError('Enter a debounce between 0 and 1440 minutes.');
      return;
    }
    if (!Number.isInteger(ttl) || ttl < 1 || ttl > 43200) {
      setDefaultsError('Enter a queue lifetime between 1 and 43200 minutes.');
      return;
    }

    setDefaultsBusy(true);
    try {
      const res = await api.put('/api/cpo/profile', {
        queued_charging_enabled: queuedEnabled,
        auto_start_delay_min: delay,
        queue_ttl_min: ttl,
      });
      setTenant((prev) => ({ ...prev, ...res }));
      setQueuedEnabled(Boolean(res.queued_charging_enabled));
      setAutoStartDelay(String(res.auto_start_delay_min));
      setQueueTtl(String(res.queue_ttl_min));
      toast.ok('Charging defaults saved.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setDefaultsBusy(false);
    }
  };

  const gstinTrimmed = gstin.trim();
  const gstinLooksValid = !gstinTrimmed || GSTIN_RE.test(gstinTrimmed.toUpperCase());

  const saveGst = async (e) => {
    e.preventDefault();
    setGstBusy(true);
    try {
      const res = await api.put('/api/cpo/profile', {
        gstin: gstinTrimmed,
        legal_name: legalName.trim(),
        invoice_prefix: invoicePrefix.trim(),
      });
      setTenant((prev) => ({ ...prev, ...res }));
      setGstin(res.gstin || '');
      setLegalName(res.legal_name || '');
      setInvoicePrefix(res.invoice_prefix || '');
      toast.ok('Invoicing details saved.');
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setGstBusy(false);
    }
  };

  return (
    <CpoLayout>
      <PageHeader
        eyebrow="Console"
        title="Settings"
        sub="Your organization's identity, queued-charge defaults and GST invoicing."
      />

      {loading ? (
        <div className="stack" aria-hidden="true">
          <div className="card settings-card">
            <SkeletonTitle />
            <Skeleton lines={2} />
          </div>
          <div className="card settings-card">
            <SkeletonTitle />
            <Skeleton lines={3} />
          </div>
          <div className="card settings-card">
            <SkeletonTitle />
            <Skeleton lines={3} />
          </div>
        </div>
      ) : error ? (
        <ErrorState error={error} onRetry={fetchProfile} title="Couldn't load your settings" />
      ) : (
        <div className="stack">
          <section className="card settings-card">
            <h2 className="settings-card-title">
              <Building2 size={18} aria-hidden="true" />
              Organization
            </h2>
            <div className="field">
              <span className="field-label">Organization name</span>
              <p className="settings-readonly">{tenant?.name || '—'}</p>
              <p className="field-help">To rename your organization, contact support.</p>
            </div>
            <div className="field">
              <span className="field-label">Timezone</span>
              <p className="settings-readonly">{tenant?.timezone || '—'}</p>
              <p className="field-help">
                The wall-clock zone used to interpret your pricing plans' time-of-day slots.
              </p>
            </div>
          </section>

          <section className="card settings-card">
            <h2 className="settings-card-title">
              <Zap size={18} aria-hidden="true" />
              Charging defaults
            </h2>
            <form className="stack" onSubmit={saveDefaults}>
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={queuedEnabled}
                  onChange={(e) => setQueuedEnabled(e.target.checked)}
                />
                Enable queued charging by default
              </label>
              <p className="field-help">
                New chargers inherit this unless they carry their own override. Off by default.
              </p>

              <div className="settings-field-row">
                <div className="field">
                  <label className="field-label" htmlFor="auto-start-delay">
                    Auto-start debounce (minutes)
                  </label>
                  <input
                    id="auto-start-delay"
                    type="number"
                    className="input"
                    inputMode="numeric"
                    value={autoStartDelay}
                    onChange={(e) => setAutoStartDelay(e.target.value)}
                  />
                  <p className="field-help">
                    How long a charger must see continuous power before a queued charge auto-starts.
                  </p>
                </div>
                <div className="field">
                  <label className="field-label" htmlFor="queue-ttl">
                    Queue lifetime (minutes)
                  </label>
                  <input
                    id="queue-ttl"
                    type="number"
                    className="input"
                    inputMode="numeric"
                    value={queueTtl}
                    onChange={(e) => setQueueTtl(e.target.value)}
                  />
                  <p className="field-help">
                    How long a waiting queued charge lasts before it expires (up to 30 days).
                  </p>
                </div>
              </div>

              {defaultsError && (
                <p className="field-error" role="alert">
                  {defaultsError}
                </p>
              )}

              <div>
                <button type="submit" className="btn btn-primary btn-sm" disabled={defaultsBusy}>
                  {defaultsBusy ? 'Saving…' : 'Save changes'}
                </button>
              </div>
            </form>
          </section>

          <section className="card settings-card">
            <h2 className="settings-card-title">
              <Receipt size={18} aria-hidden="true" />
              GST & invoicing
            </h2>
            <p className="text-2 text-sm">
              These details appear on the GST tax invoices drivers can download for their charging
              sessions. Leave a field blank to omit it from generated invoices.
            </p>
            <form className="stack" onSubmit={saveGst}>
              <div className="field">
                <label className="field-label" htmlFor="gstin">
                  GSTIN
                </label>
                <input
                  id="gstin"
                  type="text"
                  className="input"
                  maxLength={15}
                  placeholder="22AAAAA0000A1Z5"
                  value={gstin}
                  onChange={(e) => setGstin(e.target.value.toUpperCase())}
                  aria-describedby={!gstinLooksValid ? 'gstin-warning' : undefined}
                />
                {!gstinLooksValid && (
                  <p id="gstin-warning" className="settings-warn-text" role="status">
                    Doesn't look like a standard GSTIN — you can still save it.
                  </p>
                )}
              </div>
              <div className="field">
                <label className="field-label" htmlFor="legal-name">
                  Legal name (as on the GST certificate)
                </label>
                <input
                  id="legal-name"
                  type="text"
                  className="input"
                  maxLength={120}
                  placeholder="Acme Charging Pvt Ltd"
                  value={legalName}
                  onChange={(e) => setLegalName(e.target.value)}
                />
              </div>
              <div className="field">
                <label className="field-label" htmlFor="invoice-prefix">
                  Invoice number prefix
                </label>
                <input
                  id="invoice-prefix"
                  type="text"
                  className="input"
                  maxLength={12}
                  placeholder="ACME"
                  value={invoicePrefix}
                  onChange={(e) => setInvoicePrefix(e.target.value)}
                />
                <div className="banner banner-warn">
                  Changing the prefix only affects invoices issued after you save — numbers already
                  issued keep their original prefix.
                </div>
              </div>

              <div>
                <button type="submit" className="btn btn-primary btn-sm" disabled={gstBusy}>
                  {gstBusy ? 'Saving…' : 'Save changes'}
                </button>
              </div>
            </form>
          </section>
        </div>
      )}
    </CpoLayout>
  );
};

export default CpoSettings;
