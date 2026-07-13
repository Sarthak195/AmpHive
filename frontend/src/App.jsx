/**
 * AmpHive App Component
 * =====================
 * Root router with all page routes.
 * Includes protected route wrapper that redirects unauthenticated users
 * to the login page for protected routes.
 *
 * CPO routes (added in Phase 2.5) are gated by a CpoProtectedRoute
 * wrapper that checks the user's role. Non-CPO users accessing /cpo
 * are shown the setup page instead.
 */

import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import Navbar from './components/Navbar';
import Home from './pages/Home';
import TopUp from './pages/TopUp';
import Session from './pages/Session';
import Login from './pages/Login';
import Groups from './pages/Groups';
import History from './pages/History';
import { ProtectedRoute, CpoProtectedRoute } from './components/ProtectedRoutes';

// CPO Admin Dashboard pages
import CpoSetup from './pages/cpo/CpoSetup';
import CpoDashboard from './pages/cpo/CpoDashboard';
import CpoGateways from './pages/cpo/CpoGateways';
import CpoPlugs from './pages/cpo/CpoPlugs';
import CpoGroups from './pages/cpo/CpoGroups';
import CpoSessions from './pages/cpo/CpoSessions';
import CpoReservations from './pages/cpo/CpoReservations';
import CpoFaults from './pages/cpo/CpoFaults';
import CpoEarnings from './pages/cpo/CpoEarnings';

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
          <Route path="/history" element={
            <ProtectedRoute><History /></ProtectedRoute>
          } />

          {/* CPO Setup — requires auth, shows onboarding if not a CPO */}
          <Route path="/cpo" element={
            <ProtectedRoute><CpoSetup /></ProtectedRoute>
          } />

          {/* CPO Dashboard routes — require auth + CPO role */}
          <Route path="/cpo/dashboard" element={
            <CpoProtectedRoute><CpoDashboard /></CpoProtectedRoute>
          } />
          <Route path="/cpo/gateways" element={
            <CpoProtectedRoute><CpoGateways /></CpoProtectedRoute>
          } />
          <Route path="/cpo/plugs" element={
            <CpoProtectedRoute><CpoPlugs /></CpoProtectedRoute>
          } />
          <Route path="/cpo/groups" element={
            <CpoProtectedRoute><CpoGroups /></CpoProtectedRoute>
          } />
          <Route path="/cpo/sessions" element={
            <CpoProtectedRoute><CpoSessions /></CpoProtectedRoute>
          } />
          <Route path="/cpo/reservations" element={
            <CpoProtectedRoute><CpoReservations /></CpoProtectedRoute>
          } />
          <Route path="/cpo/faults" element={
            <CpoProtectedRoute><CpoFaults /></CpoProtectedRoute>
          } />
          <Route path="/cpo/earnings" element={
            <CpoProtectedRoute><CpoEarnings /></CpoProtectedRoute>
          } />

          {/* Catch-all — redirect to home */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </div>
    </Router>
  );
}

export default App;

