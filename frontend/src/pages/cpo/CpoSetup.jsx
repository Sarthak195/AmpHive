/**
 * CpoSetup (redesign v3, D8) — one-time onboarding for a driver who wants to
 * become a Charge Point Operator: creates a new tenant and promotes the
 * user's role from 'driver' to 'cpo'. Shown at /cpo for a user who doesn't
 * have the 'cpo' role yet; already-CPO users are redirected past it.
 *
 * A signed-in cpo (or an admin who already has a tenant_id) is redirected
 * straight to the console; a plain admin with no tenant has nothing to set
 * up here and is sent to /admin instead.
 *
 * Data: POST /api/cpo/setup { tenant_name }.
 *
 * Console pages live under data-theme="volt", but this page renders before
 * the user has a tenant (so it can't mount inside CpoLayout) — it stamps the
 * volt theme itself and uses an AuthShell-like centered card, restyled for
 * the console rather than reusing the driver-surface AuthShell component.
 */

import { useState } from 'react';
import { useNavigate, Navigate } from 'react-router-dom';
import { PlugZap, Tags, Users, Zap } from 'lucide-react';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../components/ui';
import api from '../../api/client';
import { apiErrorCopy } from '../../utils/statusCopy';
import useTheme from '../../hooks/useTheme';
import './CpoSetup.css';

const NEXT_STEPS = [
  { icon: PlugZap, title: 'Add your chargers', body: 'Register gateways and plugs in Chargers.' },
  { icon: Tags, title: 'Set your pricing', body: 'Create a tariff and its rates in Pricing.' },
  { icon: Users, title: 'Create groups', body: 'Organize chargers and invite drivers in Groups.' },
];

const CpoSetup = () => {
  useTheme('volt');
  const toast = useToast();
  const [tenantName, setTenantName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const navigate = useNavigate();
  const { user, refreshUser } = useAuth();

  // If the user already has a console to go to, redirect past this form. Use
  // the declarative <Navigate> element instead of calling navigate() in the
  // render body — an imperative navigate during render updates the router
  // mid-render, which React warns about and can loop under StrictMode.
  if (user?.role === 'cpo' || (user?.role === 'admin' && user?.tenant_id)) {
    return <Navigate to="/cpo/dashboard" replace />;
  }
  if (user?.role === 'admin') {
    return <Navigate to="/admin" replace />;
  }

  const handleSubmit = async (e) => {
    e.preventDefault();
    const name = tenantName.trim();
    if (!name) {
      setError('Enter your organization name.');
      return;
    }

    setError('');
    setBusy(true);
    try {
      await api.post('/api/cpo/setup', { tenant_name: name });
      await refreshUser();
      toast.ok(`Organization "${name}" created.`);
      navigate('/cpo/dashboard', { replace: true });
    } catch (err) {
      setError(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="cposetup-shell">
      <div className="cposetup-inner">
        <span className="brand cposetup-brand">
          <span className="brand-bolt">
            <Zap size={16} aria-hidden="true" />
          </span>
          AmpHive Console
        </span>

        <div className="card cposetup-card">
          <h1 className="cposetup-title">Set up your organization</h1>
          <p className="cposetup-sub">
            Manage EV charging plugs, create charger groups and earn revenue from charging
            sessions.
          </p>

          <form className="stack" onSubmit={handleSubmit}>
            <div className="field">
              <label className="field-label" htmlFor="tenant-name">
                Organization name
              </label>
              <input
                id="tenant-name"
                type="text"
                className="input"
                placeholder="e.g. Sunrise Apartments, TechPark Office"
                value={tenantName}
                onChange={(e) => setTenantName(e.target.value)}
                maxLength={100}
                autoFocus
              />
            </div>

            {error && (
              <div className="banner banner-danger" role="alert">
                {error}
              </div>
            )}

            <button type="submit" className="btn btn-primary btn-full" disabled={busy}>
              {busy ? 'Creating…' : 'Create organization'}
            </button>
          </form>

          <div className="cposetup-checklist">
            <h2>What happens next</h2>
            <ol>
              {NEXT_STEPS.map(({ icon: Icon, title, body }, i) => (
                <li key={title}>
                  <span className="cposetup-step" aria-hidden="true">
                    <Icon size={14} />
                  </span>
                  <span>
                    <strong>
                      {i + 1}. {title}
                    </strong>
                    <br />
                    {body}
                  </span>
                </li>
              ))}
            </ol>
          </div>
        </div>
      </div>
    </main>
  );
};

export default CpoSetup;
