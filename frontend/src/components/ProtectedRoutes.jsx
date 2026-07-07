/**
 * Route guards (extracted from App.jsx so they can be unit-tested).
 *
 * ProtectedRoute    — requires a signed-in user; otherwise → /login.
 * CpoProtectedRoute — requires the 'cpo' or 'admin' role; authenticated
 *                     non-CPOs → /cpo (setup/onboarding page); anonymous
 *                     users → /login.
 */

import { Navigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

export const ProtectedRoute = ({ children }) => {
  const { user } = useAuth();
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return children;
};

export const CpoProtectedRoute = ({ children }) => {
  const { user } = useAuth();
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  if (user.role !== 'cpo' && user.role !== 'admin') {
    return <Navigate to="/cpo" replace />;
  }
  return children;
};
