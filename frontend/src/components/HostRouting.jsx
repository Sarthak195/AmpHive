/**
 * Host-partition routing pieces (see utils/appHost.js).
 *
 * ExternalRedirect — routes that belong on the OTHER hostname render this:
 *                    it hard-navigates (window.location.replace) to the same
 *                    path on the counterpart origin, showing a one-line
 *                    "moved" note in the meantime (also what tests assert on,
 *                    since jsdom can't actually navigate).
 * CpoLanding       — the CPO host's "/" route: anonymous → /login; admin →
 *                    /admin; cpo → /cpo/dashboard; a driver-role login gets
 *                    a clear "not an operator account" message with links to
 *                    the driver origin and to the /cpo become-a-host flow.
 */

import { useEffect } from 'react';
import { Navigate, useLocation, Link } from 'react-router-dom';
import { UserX } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';
import { driverOrigin } from '../utils/appHost';

export const ExternalRedirect = ({ origin }) => {
  const location = useLocation();
  const target = `${origin}${location.pathname}${location.search}${location.hash}`;
  useEffect(() => {
    window.location.replace(target);
  }, [target]);
  return (
    <main className="page">
      <div className="state-block">
        <p>
          This page has moved. <a href={target}>Continue to {origin}</a>
        </p>
      </div>
    </main>
  );
};

export const CpoLanding = () => {
  const { user } = useAuth();
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  if (user.role === 'admin') {
    return <Navigate to="/admin" replace />;
  }
  if (user.role === 'cpo') {
    return <Navigate to="/cpo/dashboard" replace />;
  }
  // Driver-role account on the operator portal.
  return (
    <main className="page">
      <div className="card state-block anim-fade">
        <UserX className="state-icon" aria-hidden="true" />
        <h1>This account is not an operator account</h1>
        <p>
          You're signed in as a driver. The operator portal is for charge
          point operators (hosts).
        </p>
        <div className="page-actions">
          <a href={driverOrigin()} className="btn btn-quiet">Go to the driver app</a>
          <Link to="/cpo" className="btn btn-primary">Apply to become a host</Link>
        </div>
      </div>
    </main>
  );
};
