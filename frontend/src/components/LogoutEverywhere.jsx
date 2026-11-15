/**
 * LogoutEverywhere: the "Log out everywhere" control plus its confirm step.
 *
 * The normal Log out button signs out only this app in this browser (the
 * server revokes that one sign-in session). This is the global revoke: every
 * device and browser this account is signed in on, including this one, is
 * signed out (POST /api/auth/logout-everywhere bumps token_version).
 *
 * Mounted in two places so every role can reach it on its own origin: the
 * driver Account page (Security card) and CPO Settings (cpo + admin, on the
 * CPO host, whose localStorage the driver origin cannot see).
 *
 * Not best-effort: if the server call fails, the other devices are still
 * signed in, so the dialog stays open with the error and this session is
 * kept. On success a full-page replace to /login drops every provider's
 * state for the old session and keeps the page out of the back stack.
 */

import { useState } from 'react';
import { LogOut } from 'lucide-react';
import Modal from './ui/Modal';
import { useAuth } from '../contexts/AuthContext';
import { apiErrorCopy } from '../utils/statusCopy';

export default function LogoutEverywhere() {
  const { logoutEverywhere } = useAuth();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const openDialog = () => {
    setError('');
    setOpen(true);
  };

  const dismiss = () => {
    if (!busy) setOpen(false);
  };

  const confirm = async () => {
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await logoutEverywhere();
      window.location.replace('/login');
    } catch (err) {
      setError(apiErrorCopy(err));
      setBusy(false);
    }
  };

  return (
    <div className="stack-sm">
      <p className="text-2 text-sm">
        Log out signs out this app in this browser only. Signed in on a phone or
        browser you no longer use, or think someone else has your session? Log out
        everywhere signs you out on every device, including this one.
      </p>
      <div>
        <button type="button" className="btn btn-quiet" onClick={openDialog}>
          <LogOut size={16} aria-hidden="true" />
          Log out everywhere
        </button>
      </div>

      <Modal
        open={open}
        onClose={dismiss}
        title="Log out everywhere?"
        size="sm"
        footer={
          <>
            <button type="button" className="btn btn-quiet" onClick={dismiss} disabled={busy}>
              Cancel
            </button>
            <button type="button" className="btn btn-primary" onClick={confirm} disabled={busy}>
              {busy ? 'Signing out…' : 'Log out everywhere'}
            </button>
          </>
        }
      >
        <div className="stack-sm">
          <p className="text-2">
            You&apos;ll be signed out on every device and browser, including this
            one, and need to sign in again on each.
          </p>
          {error && (
            <div className="banner banner-danger" role="alert">
              <p>{error}</p>
            </div>
          )}
        </div>
      </Modal>
    </div>
  );
}
