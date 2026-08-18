/**
 * NotFound — the 404 page.
 * ========================
 * Every unknown path used to `<Navigate to="/" replace />`: a mistyped or
 * stale URL silently became the homepage with no explanation, and — because
 * nginx serves index.html for everything — every one of those was a soft 404
 * returning HTTP 200, which invites crawlers to index unlimited garbage URLs
 * as duplicates of the homepage.
 *
 * Two halves to the fix:
 *   1. this page, which says what happened and offers a real way onward;
 *   2. `noindex` (via useDocumentMeta) plus the nginx `location` that returns a
 *      genuine 404 status for paths outside the app's known route prefixes —
 *      see frontend/nginx.conf, which is the only layer that can set a status
 *      code for a client-rendered route.
 *
 * Links are role-aware: an operator who mistypes a console URL wants the
 * console, not the driver homepage.
 */

import { Link } from 'react-router-dom';
import { Compass, Home, LifeBuoy } from 'lucide-react';
import useDocumentMeta from '../hooks/useDocumentMeta';
import { useAuth } from '../contexts/AuthContext';
import { isCpoHost } from '../utils/appHost';

export default function NotFound() {
  const { user } = useAuth();
  useDocumentMeta({
    title: 'Page not found',
    description: 'That AmpHive page does not exist.',
  });

  const console_ = isCpoHost() || user?.role === 'cpo' || user?.role === 'admin';

  return (
    <main className="page">
      <div className="card stack" style={{ textAlign: 'center', maxWidth: '32rem', margin: '0 auto' }}>
        <p className="text-3" style={{ fontSize: '2.5rem', lineHeight: 1, margin: 0 }} aria-hidden="true">
          404
        </p>
        <h1>We couldn&apos;t find that page</h1>
        <p className="text-2">
          The link may be out of date, or the address may have a typo in it.
          Nothing is broken on your side.
        </p>

        <div className="row" style={{ justifyContent: 'center', flexWrap: 'wrap', gap: '0.5rem' }}>
          {console_ ? (
            <Link className="btn btn-primary" to="/cpo/dashboard">
              <Home size={16} aria-hidden="true" /> Host console
            </Link>
          ) : (
            <>
              <Link className="btn btn-primary" to="/">
                <Home size={16} aria-hidden="true" /> Go home
              </Link>
              <Link className="btn btn-quiet" to="/map">
                <Compass size={16} aria-hidden="true" /> Find a charger
              </Link>
            </>
          )}
          <Link className="btn btn-quiet" to="/contact">
            <LifeBuoy size={16} aria-hidden="true" /> Contact us
          </Link>
        </div>
      </div>
    </main>
  );
}
