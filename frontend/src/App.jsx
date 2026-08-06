/**
 * AmpHive App Component
 * =====================
 * Root router — redesign v3. Three route trees keyed off the hostname
 * partition (utils/appHost.js):
 *
 *   driver host   driver app + marketing; /cpo/* hard-redirects to the CPO
 *                 origin (ExternalRedirect).
 *   CPO host      operator portal + platform admin; driver routes bounce
 *                 back to the driver origin.
 *   unsplit       bare-IP / localhost fallback — everything internal.
 *
 * Pages load via React.lazy so each surface (marketing, driver, console,
 * admin) ships as its own chunk; BootSplash is both the auth-restore gate
 * and the Suspense fallback. Driver chrome (AppBar + MobileTabBar) renders
 * inside the driver route trees via DriverShell — console routes bring
 * their own layout chrome, and the auth pages (Login/Signup/ForgotPassword/
 * ResetPassword) render standalone outside any shell (their own AuthShell
 * frame is the only chrome they get).
 */

import { lazy, Suspense } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate, Outlet, useLocation } from 'react-router-dom';
import AppBar from './components/AppBar';
import MobileTabBar from './components/MobileTabBar';
import BootSplash from './components/BootSplash';
import ErrorBoundary from './components/ErrorBoundary';
import AdminLayout from './components/AdminLayout';
import { ProtectedRoute, CpoProtectedRoute, AdminProtectedRoute } from './components/ProtectedRoutes';
import { ExternalRedirect, CpoLanding } from './components/HostRouting';
import { useAuth } from './contexts/AuthContext';
import { isCpoHost, isSplitHost, cpoOrigin, driverOrigin } from './utils/appHost';

// ---- lazy page chunks -----------------------------------------------------
// Marketing
const Marketing = lazy(() => import('./pages/Marketing'));
// Driver pages
const Dashboard = lazy(() => import('./pages/Dashboard'));
const MapPage = lazy(() => import('./pages/MapPage'));
const Session = lazy(() => import('./pages/Session'));
const Wallet = lazy(() => import('./pages/Wallet'));
const Activity = lazy(() => import('./pages/Activity'));
const Groups = lazy(() => import('./pages/Groups'));
const Account = lazy(() => import('./pages/Account'));
const Terms = lazy(() => import('./pages/Terms'));
const Login = lazy(() => import('./pages/Login'));
const Signup = lazy(() => import('./pages/Signup'));
const ForgotPassword = lazy(() => import('./pages/ForgotPassword'));
const ResetPassword = lazy(() => import('./pages/ResetPassword'));
const GoogleCallback = lazy(() => import('./pages/GoogleCallback'));
// Console (CPO) pages
const CpoSetup = lazy(() => import('./pages/cpo/CpoSetup'));
const CpoDashboard = lazy(() => import('./pages/cpo/CpoDashboard'));
const CpoGateways = lazy(() => import('./pages/cpo/CpoGateways'));
const CpoChargers = lazy(() => import('./pages/cpo/CpoChargers'));
const CpoGroups = lazy(() => import('./pages/cpo/CpoGroups'));
const CpoSessions = lazy(() => import('./pages/cpo/CpoSessions'));
const CpoReservations = lazy(() => import('./pages/cpo/CpoReservations'));
const CpoHealth = lazy(() => import('./pages/cpo/CpoHealth'));
const CpoEarnings = lazy(() => import('./pages/cpo/CpoEarnings'));
const CpoPricing = lazy(() => import('./pages/cpo/CpoPricing'));
const CpoInvoices = lazy(() => import('./pages/cpo/CpoInvoices'));
const CpoDisputes = lazy(() => import('./pages/cpo/CpoDisputes'));
const CpoPlugReports = lazy(() => import('./pages/cpo/CpoPlugReports'));
const CpoSettings = lazy(() => import('./pages/cpo/CpoSettings'));
// Admin pages
const AdminOverview = lazy(() => import('./pages/admin/AdminOverview'));
const AdminTenants = lazy(() => import('./pages/admin/AdminTenants'));
const AdminTenantDetail = lazy(() => import('./pages/admin/AdminTenantDetail'));
const AdminUsers = lazy(() => import('./pages/admin/AdminUsers'));
const AdminPayouts = lazy(() => import('./pages/admin/AdminPayouts'));
const AdminGateways = lazy(() => import('./pages/admin/AdminGateways'));
const AdminPlugs = lazy(() => import('./pages/admin/AdminPlugs'));
const AdminFirmwareReleases = lazy(() => import('./pages/admin/AdminFirmwareReleases'));
const AdminDisputes = lazy(() => import('./pages/admin/AdminDisputes'));
const AdminAudit = lazy(() => import('./pages/admin/AdminAudit'));

/** Driver-host "/": Dashboard when signed in, Marketing otherwise. The
    printed-QR deep link `/?plug=<id>` keeps working: anonymous visitors go
    to login with the full location preserved as state.from (Login returns
    them here), signed-in drivers land on Dashboard which reads ?plug=. */
const HomeGate = () => {
  const { user } = useAuth();
  const location = useLocation();
  if (user) return <Dashboard />;
  if (new URLSearchParams(location.search).get('plug')) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }
  return <Marketing />;
};

/** Driver chrome: AppBar on top, bottom tab bar for signed-in users, and
    .has-tabbar padding so page content never hides behind the tabs. */
const DriverShell = () => {
  const { user } = useAuth();
  return (
    <div className={user ? 'has-tabbar' : undefined}>
      <AppBar />
      <Outlet />
      {user && <MobileTabBar />}
    </div>
  );
};

/** Password reset — public on every host (operators forget passwords too);
    the emailed link lands on /reset-password?token=... */
const passwordResetRoutes = (
  <>
    <Route path="/forgot-password" element={<ForgotPassword />} />
    <Route path="/reset-password" element={<ResetPassword />} />
  </>
);

/** Auth pages render standalone — no AppBar/MobileTabBar chrome. They bring
    their own AuthShell frame, so mounting them inside DriverShell would show
    double chrome (this app bar AND the auth card's own header). */
const authRoutes = (
  <>
    <Route path="/login" element={<Login />} />
    <Route path="/signup" element={<Signup />} />
    {passwordResetRoutes}
    {/* "Sign in with Google" lands here with a single-use, nonce-bound
        exchange code in the URL fragment (#code=...) that GoogleCallback
        POSTs to /api/auth/google/exchange for the app JWT — see
        backend/routers/auth.py google_callback and pages/GoogleCallback.jsx. */}
    <Route path="/auth/google/callback" element={<GoogleCallback />} />
  </>
);

/** Driver experience — shared verbatim by the driver host and the unsplit
    fallback tree. Legacy paths redirect: /topup & /wallet → /credit, /history →
    /activity. */
const driverRoutes = (
  <>
    {authRoutes}
    <Route element={<DriverShell />}>
      <Route path="/" element={<HomeGate />} />
      {/* Public charger-discovery map — no account needed. */}
      <Route path="/map" element={<MapPage />} />
      {/* Public charging-credit terms — no account needed. */}
      <Route path="/terms" element={<Terms />} />
      <Route path="/session" element={
        <ProtectedRoute><Session /></ProtectedRoute>
      } />
      <Route path="/credit" element={
        <ProtectedRoute><Wallet /></ProtectedRoute>
      } />
      <Route path="/activity" element={
        <ProtectedRoute><Activity /></ProtectedRoute>
      } />
      <Route path="/groups" element={
        <ProtectedRoute><Groups /></ProtectedRoute>
      } />
      <Route path="/account" element={
        <ProtectedRoute><Account /></ProtectedRoute>
      } />
      {/* Renamed pages keep their old URLs working. */}
      <Route path="/topup" element={<Navigate to="/credit" replace />} />
      <Route path="/wallet" element={<Navigate to="/credit" replace />} />
      <Route path="/history" element={<Navigate to="/activity" replace />} />
    </Route>
  </>
);

/** CPO console route table — shared by the CPO host and unsplit trees.
    Renamed sections redirect: plugs → chargers, faults → health,
    tariffs → pricing. */
const cpoDashboardRoutes = (
  <>
    <Route path="/cpo/dashboard" element={
      <CpoProtectedRoute><CpoDashboard /></CpoProtectedRoute>
    } />
    <Route path="/cpo/gateways" element={
      <CpoProtectedRoute><CpoGateways /></CpoProtectedRoute>
    } />
    <Route path="/cpo/chargers" element={
      <CpoProtectedRoute><CpoChargers /></CpoProtectedRoute>
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
    <Route path="/cpo/health" element={
      <CpoProtectedRoute><CpoHealth /></CpoProtectedRoute>
    } />
    <Route path="/cpo/earnings" element={
      <CpoProtectedRoute><CpoEarnings /></CpoProtectedRoute>
    } />
    <Route path="/cpo/pricing" element={
      <CpoProtectedRoute><CpoPricing /></CpoProtectedRoute>
    } />
    <Route path="/cpo/invoices" element={
      <CpoProtectedRoute><CpoInvoices /></CpoProtectedRoute>
    } />
    <Route path="/cpo/disputes" element={
      <CpoProtectedRoute><CpoDisputes /></CpoProtectedRoute>
    } />
    <Route path="/cpo/plug-reports" element={
      <CpoProtectedRoute><CpoPlugReports /></CpoProtectedRoute>
    } />
    <Route path="/cpo/settings" element={
      <CpoProtectedRoute><CpoSettings /></CpoProtectedRoute>
    } />
    <Route path="/cpo/plugs" element={<Navigate to="/cpo/chargers" replace />} />
    <Route path="/cpo/faults" element={<Navigate to="/cpo/health" replace />} />
    <Route path="/cpo/tariffs" element={<Navigate to="/cpo/pricing" replace />} />
  </>
);

/** Platform-admin routes — admin role only; AdminLayout stamps the volt
    theme + admin accent. Registered on the CPO host and unsplit trees. */
const adminRoutes = (
  <Route element={<AdminProtectedRoute><AdminLayout /></AdminProtectedRoute>}>
    <Route path="/admin" element={<AdminOverview />} />
    <Route path="/admin/tenants" element={<AdminTenants />} />
    <Route path="/admin/tenants/:id" element={<AdminTenantDetail />} />
    <Route path="/admin/users" element={<AdminUsers />} />
    <Route path="/admin/payouts" element={<AdminPayouts />} />
    <Route path="/admin/gateways" element={<AdminGateways />} />
    <Route path="/admin/chargers" element={<AdminPlugs />} />
    <Route path="/admin/firmware-releases" element={<AdminFirmwareReleases />} />
    <Route path="/admin/disputes" element={<AdminDisputes />} />
    <Route path="/admin/audit" element={<AdminAudit />} />
  </Route>
);

/** Route tree for the CPO host: operator portal + admin only; driver routes
    bounce to the driver origin. */
const CpoHostRoutes = () => (
  <Routes>
    {/* Landing: anonymous → login; role-routed after that (CpoLanding). */}
    <Route path="/" element={<CpoLanding />} />
    <Route path="/login" element={<Login />} />
    {passwordResetRoutes}

    {/* CPO self-serve setup ("Become a host") lives on this host. */}
    <Route path="/cpo" element={
      <ProtectedRoute><CpoSetup /></ProtectedRoute>
    } />
    {cpoDashboardRoutes}
    {adminRoutes}

    {/* Driver-only routes → driver origin (same path preserved). */}
    {['/map', '/terms', '/session', '/credit', '/wallet', '/activity', '/groups', '/account', '/signup', '/topup', '/history'].map((path) => (
      <Route key={path} path={path} element={<ExternalRedirect origin={driverOrigin()} />} />
    ))}

    {/* Catch-all — redirect to the portal landing. */}
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
);

/** Route tree for the driver host: everything except the operator portal;
    /cpo/* and /admin/* bounce to the CPO origin. */
const DriverHostRoutes = () => (
  <Routes>
    {driverRoutes}

    {/* Operator portal + admin live on the CPO origin — hard-redirect. */}
    <Route path="/cpo/*" element={<ExternalRedirect origin={cpoOrigin()} />} />
    <Route path="/cpo" element={<ExternalRedirect origin={cpoOrigin()} />} />
    <Route path="/admin/*" element={<ExternalRedirect origin={cpoOrigin()} />} />
    <Route path="/admin" element={<ExternalRedirect origin={cpoOrigin()} />} />

    {/* Catch-all — redirect to home */}
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
);

/** Unsplit hosts (bare IP DNS-outage fallback, localhost dev): the combined
    tree — internal /cpo and /admin, no cross-origin redirects. Without this
    a DNS outage would lock operators out of the portal entirely. */
const UnsplitRoutes = () => (
  <Routes>
    {driverRoutes}
    <Route path="/cpo" element={
      <ProtectedRoute><CpoSetup /></ProtectedRoute>
    } />
    {cpoDashboardRoutes}
    {adminRoutes}
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes>
);

function App() {
  const { loading } = useAuth();

  // Hold the whole tree behind the splash until the session restore
  // settles — guards no longer flash anonymous redirects on reload.
  if (loading) return <BootSplash />;

  return (
    <Router>
      <ErrorBoundary>
        <Suspense fallback={<BootSplash />}>
          {!isSplitHost() ? <UnsplitRoutes />
            : isCpoHost() ? <CpoHostRoutes /> : <DriverHostRoutes />}
        </Suspense>
      </ErrorBoundary>
    </Router>
  );
}

export default App;
