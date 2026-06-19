/**
 * AmpHive App Component
 * =====================
 * Root router with all page routes.
 * Includes protected route wrapper that redirects unauthenticated users
 * to the login page for protected routes.
 */

import React from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import Navbar from './components/Navbar';
import Home from './pages/Home';
import TopUp from './pages/TopUp';
import Session from './pages/Session';
import Login from './pages/Login';
import Groups from './pages/Groups';
import { useAuth } from './contexts/AuthContext';

/**
 * Protected route wrapper — redirects to /login if user is not authenticated.
 * Used for routes that require a signed-in user (TopUp, Session, Groups).
 */
const ProtectedRoute = ({ children }) => {
  const { user } = useAuth();
  if (!user) {
    return <Navigate to="/login" replace />;
  }
  return children;
};

function App() {
  return (
    <Router>
      <div style={{ minHeight: '100vh' }}>
        <Navbar />
        <Routes>
          {/* Public routes */}
          <Route path="/" element={<Home />} />
          <Route path="/login" element={<Login />} />

          {/* Protected routes — require authentication */}
          <Route path="/topup" element={
            <ProtectedRoute><TopUp /></ProtectedRoute>
          } />
          <Route path="/session" element={
            <ProtectedRoute><Session /></ProtectedRoute>
          } />
          <Route path="/groups" element={
            <ProtectedRoute><Groups /></ProtectedRoute>
          } />

          {/* Catch-all — redirect to home */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </Router>
  );
}

export default App;
