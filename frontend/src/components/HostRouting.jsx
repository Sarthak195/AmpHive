/**
 * Host-partition routing pieces (see utils/appHost.js).
 *
 * ExternalRedirect — routes that belong on the OTHER hostname render this:
 *                    it hard-navigates (window.location.replace) to the same
 *                    path on the counterpart origin, showing a one-line
 *                    "moved" note in the meantime (also what tests assert on,
 *                    since jsdom can't actually navigate).
 * CpoLanding       — the CPO host's "/" route: anonymous → /login; cpo/admin
 *                    → /cpo/dashboard; a driver-role login gets a clear
 *                    "not an operator account" message with links to the
 *                    driver origin and to the /cpo become-a-host flow.
 */

import { useEffect } from 'react';
import { Navigate, useLocation, Link } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';
import { driverOrigin } from '../utils/appHost';

export const ExternalRedirect = ({ origin }) => {
  const location = useLocation();
  const target = `${origin}${location.pathname}${location.search}${location.hash}`;
  useEffect(() => {
    window.location.replace(target);
  }, [target]);
  return (
    <div className="page-container text-center" style={{ marginTop: '4rem' }}>
      <p>
        This page has moved. <a href={target}>Continue to {origin}</a>
      </p>
    </div>
  );
};

export const CpoLanding = () => {
  const { user } = useAuth();
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  if (user.role === 'cpo' || user.role === 'admin') {
    return <Navigate to="/cpo/dashboard" replace />;
  }
  // Driver-role account on the operator portal.
  return (
    <div className="page-container animate-fade-in text-center" style={{ maxWidth: '520px', marginTop: '4rem' }}>
      <div className="glass glass-panel">
        <h2 style={{ marginBottom: '1rem' }}>This account is not an operator account</h2>
        <p style={{ marginBottom: '1.5rem' }}>
          You are signed in as a driver. The operator portal is for charge
          point operators (hosts).
        </p>
        <p style={{ marginBottom: '0.75rem' }}>
          <a href={driverOrigin()} className="nav-link">Go to the driver app</a>
        </p>
        <p>
          <Link to="/cpo" className="nav-link">Apply to become a host</Link>
        </p>
      </div>
    </div>
  );
};
