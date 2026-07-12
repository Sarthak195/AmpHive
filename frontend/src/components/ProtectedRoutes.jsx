/**
 * Route guards (extracted from App.jsx so they can be unit-tested).
 *
 * ProtectedRoute    — requires a signed-in user; otherwise → /login.
 * CpoProtectedRoute — requires the 'cpo' or 'admin' role; authenticated
 *                     non-CPOs → /cpo (setup/onboarding page); anonymous
 *                     users → /login.
 *
 * Both anonymous-redirect branches carry the current location in router
 * state (`state.from`) so Login can send the driver back to exactly where
 * they started (e.g. a QR/deep-link `/?plug=<id>`) instead of always
 * bouncing to Home — see Login.jsx.
 */

import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

export const ProtectedRoute = ({ children }) => {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return children;
};

export const CpoProtectedRoute = ({ children }) => {
  const { user } = useAuth();
  const location = useLocation();
  if (!user) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  if (user.role !== 'cpo' && user.role !== 'admin') {
    return <Navigate to="/cpo" replace />;
  }
  return children;
};
