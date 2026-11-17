/**
 * ApiTokens: personal read-only API tokens for Home Assistant or evcc (F7).
 *
 *   GET    /api/me/tokens       -> { tokens: [{id, name, prefix, created_at, last_used_at}], max_active }
 *   POST   /api/me/tokens       {name} -> {id, name, token, prefix, created_at}
 *   DELETE /api/me/tokens/{id}  revoke
 *
 * The token itself is in the POST response only: it is shown once, with a
 * copy button, and never fetched again (the server keeps only its hash). A
 * token can only read GET /api/me/meter; see docs/HOME_ASSISTANT.md.
 *
 * Mounted on the driver Account page and in CPO Settings (the operator host
 * cannot reach /account). A read-only cpo_viewer cannot create tokens, so
 * the panel is not shown to one.
 */

import { useCallback, useEffect, useState } from 'react';
import { Copy, KeyRound } from 'lucide-react';
import ConfirmDialog from './ui/ConfirmDialog';
import { useToast } from './ui';
import api from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { apiErrorCopy } from '../utils/statusCopy';
import './ApiTokens.css';

const formatDate = (iso) => {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return d.toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' });
};

export default function ApiTokens({ className = 'card', titleClassName = 'account-card-title' }) {
  const { user } = useAuth();
  const toast = useToast();
  const [tokens, setTokens] = useState([]);
  const [maxActive, setMaxActive] = useState(5);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState(null);
  const [revoking, setRevoking] = useState(null);
  const [revokeBusy, setRevokeBusy] = useState(false);

  const isViewer = user?.role === 'cpo_viewer';

  const load = useCallback(async () => {
    try {
      const res = await api.get('/api/me/tokens');
      setTokens(Array.isArray(res?.tokens) ? res.tokens : []);
      if (Number.isInteger(res?.max_active)) setMaxActive(res.max_active);
    } catch {
      setTokens([]);
    }
  }, []);

  useEffect(() => {
    if (!isViewer) load();
  }, [isViewer, load]);

  if (isViewer) return null;

  const atCap = tokens.length >= maxActive;

  const handleCreate = async (e) => {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    try {
      const res = await api.post('/api/me/tokens', { name: trimmed });
      setCreated(res);
      setName('');
      await load();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setBusy(false);
    }
  };

  const copyToken = async () => {
    if (!created?.token) return;
    try {
      await navigator.clipboard.writeText(created.token);
      toast.ok('Token copied.');
    } catch {
      toast.error('Could not copy. Select the token and copy it manually.');
    }
  };

  const confirmRevoke = async () => {
    if (!revoking) return;
    setRevokeBusy(true);
    try {
      await api.delete(`/api/me/tokens/${revoking.id}`);
      toast.ok('Token revoked.');
      setRevoking(null);
      await load();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setRevokeBusy(false);
    }
  };

  return (
    <section className={className} aria-labelledby="api-tokens-title">
      <h2 className={titleClassName} id="api-tokens-title">
        <KeyRound size={18} aria-hidden="true" />
        API tokens
      </h2>
      <div className="stack-sm">
        <p className="text-2 text-sm">
          Read-only tokens let Home Assistant or evcc read live power, current,
          voltage and session energy from GET /api/me/meter. A token cannot
          start, stop or change anything. Up to {maxActive} at a time.
        </p>

        {created && (
          <div className="banner banner-ok stack-sm" role="status">
            <p>
              Copy &ldquo;{created.name}&rdquo; now. It won&apos;t be shown again.
            </p>
            <code className="mono api-token-secret" data-testid="api-token-secret">
              {created.token}
            </code>
            <div className="row-between">
              <button type="button" className="btn btn-quiet btn-sm" onClick={copyToken}>
                <Copy size={14} aria-hidden="true" />
                Copy token
              </button>
              <button type="button" className="btn btn-quiet btn-sm" onClick={() => setCreated(null)}>
                Done
              </button>
            </div>
          </div>
        )}

        <form className="stack-sm" onSubmit={handleCreate}>
          <div className="field">
            <label className="field-label" htmlFor="api-token-name">
              Token name
            </label>
            <input
              id="api-token-name"
              type="text"
              className="input"
              maxLength={64}
              placeholder="Home Assistant"
              value={name}
              onChange={(e) => setName(e.target.value)}
              autoComplete="off"
            />
          </div>
          <div>
            <button
              type="submit"
              className="btn btn-quiet"
              disabled={busy || atCap || !name.trim()}
            >
              {busy ? 'Creating…' : 'Create token'}
            </button>
          </div>
          {atCap && (
            <p className="text-3 text-sm">Revoke a token to create another.</p>
          )}
        </form>

        {tokens.length > 0 && (
          <ul className="api-token-list" aria-label="Active API tokens">
            {tokens.map((t) => (
              <li key={t.id} className="row-between">
                <div>
                  <div>
                    {t.name} <span className="mono text-3 text-sm">{t.prefix}…</span>
                  </div>
                  <div className="text-3 text-xs">
                    Created {formatDate(t.created_at) || 'just now'}
                    {' · '}
                    {t.last_used_at ? `last used ${formatDate(t.last_used_at)}` : 'never used'}
                  </div>
                </div>
                <button
                  type="button"
                  className="btn btn-quiet btn-sm"
                  onClick={() => setRevoking(t)}
                  aria-label={`Revoke ${t.name}`}
                >
                  Revoke
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      <ConfirmDialog
        open={Boolean(revoking)}
        onClose={() => setRevoking(null)}
        onConfirm={confirmRevoke}
        title="Revoke this token?"
        body={`Anything using "${revoking?.name ?? ''}" stops getting readings straight away.`}
        confirmLabel="Revoke token"
        busy={revokeBusy}
        busyLabel="Revoking…"
      />
    </section>
  );
}
