/**
 * AmpHive App Component
 * =====================
 * Root router with all page routes.
 * Includes protected route wrapper that redirects unauthenticated users
 * to the login page for protected routes.
 *
 * Hostname partition (2026-07-20): the single bundle serves two hosts.
 * On the driver host (amphive.duckdns.org) only the driver experience
 * renders and /cpo/* hard-redirects to the CPO origin; on the CPO host
 * (cpo.amphive.duckdns.org, or VITE_FORCE_CPO_HOST=true for dev) only the
 * operator portal renders and driver routes hard-redirect back. See
 * utils/appHost.js and components/HostRouting.jsx.
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
import PublicMap from './pages/PublicMap';
import { ProtectedRoute, CpoProtectedRoute } from './components/ProtectedRoutes';
import { ExternalRedirect, CpoLanding } from './components/HostRouting';
import { isCpoHost, cpoOrigin, driverOrigin } from './utils/appHost';

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
import CpoTariffs from './pages/cpo/CpoTariffs';
import CpoInvoices from './pages/cpo/CpoInvoices';
import CpoDisputes from './pages/cpo/CpoDisputes';
import CpoSettings from './pages/cpo/CpoSettings';

/** CPO dashboard route table — shared by both hosts' route trees below
    (on the driver host they never render: a catch-all ExternalRedirect
    for /cpo/* shadows them). */
const cpoDashboardRoutes = (
  <>
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
    <Route path="/cpo/tariffs" element={
      <CpoProtectedRoute><CpoTariffs /></CpoProtectedRoute>
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
    <Route path="/cpo/invoices" element={
      <CpoProtectedRoute><CpoInvoices /></CpoProtectedRoute>
    } />
    <Route path="/cpo/disputes" element={
      <CpoProtectedRoute><CpoDisputes /></CpoProtectedRoute>
    } />
    <Route path="/cpo/settings" element={
      <CpoProtectedRoute><CpoSettings /></CpoProtectedRoute>
    } />
  </>
);

/** Route tree for the CPO host: operator portal only; driver routes bounce
    to the driver origin. */
const CpoHostRoutes = () => (
  <Routes>
    {/* Landing: anonymous → login; role-routed after that (CpoLanding). */}
    <Route path="/" element={<CpoLanding />} />
    <Route path="/login" element={<Login />} />

    {/* CPO self-serve setup ("Become a Host") lives on this host. */}
    <Route path="/cpo" element={
      <ProtectedRoute><CpoSetup /></ProtectedRoute>
    } />
    {cpoDashboardRoutes}

    {/* Driver-only routes → driver origin (same path preserved). */}
    {['/map', '/topup', '/session', '/groups', '/history'].map((path) => (
      <Route key={path} path={path} element={<ExternalRedirect origin={driverOrigin()} />} />
    ))}

    {/* Catch-all — redirect to the portal landing. */}
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
);

/** Route tree for the driver host: everything except the operator portal;
    /cpo/* bounces to the CPO origin. */
const DriverHostRoutes = () => (
  <Routes>
    {/* Public routes */}
    <Route path="/" element={<Home />} />
    <Route path="/login" element={<Login />} />
    {/* Public charger-discovery map — browse nearby public chargers
        without an account (starting a charge still routes to sign-in). */}
    <Route path="/map" element={<PublicMap />} />

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

    {/* Operator portal moved to the CPO origin — hard-redirect, path kept. */}
    <Route path="/cpo/*" element={<ExternalRedirect origin={cpoOrigin()} />} />
    <Route path="/cpo" element={<ExternalRedirect origin={cpoOrigin()} />} />

    {/* Catch-all — redirect to home */}
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
);

function App() {
  return (
    <Router>
      <div style={{ minHeight: '100vh' }}>
        <Navbar />
        {isCpoHost() ? <CpoHostRoutes /> : <DriverHostRoutes />}
      </div>
    </Router>
  );
}

export default App;
