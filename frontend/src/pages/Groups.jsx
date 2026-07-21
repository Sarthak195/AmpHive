/**
 * Groups — join a private charger group by access code, browse every group
 * the driver already has access to (public + joined-private via
 * GET /api/groups/my), and leave a private group (DELETE
 * /api/groups/{id}/leave — redesign/ui-v3 contract §4 "Driver gaps"; public
 * groups have no membership to leave, so the action is private-only).
 */

import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Users } from 'lucide-react';
import PageHeader from '../components/ui/PageHeader';
import EmptyState from '../components/ui/EmptyState';
import ErrorState from '../components/ui/ErrorState';
import Skeleton from '../components/ui/Skeleton';
import ConfirmDialog from '../components/ui/ConfirmDialog';
import { useToast } from '../components/ui';
import api from '../api/client';
import { apiErrorCopy } from '../utils/statusCopy';
import './Groups.css';

export default function Groups() {
  const toast = useToast();

  const [groups, setGroups] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const [code, setCode] = useState('');
  const [joining, setJoining] = useState(false);

  const [leaveTarget, setLeaveTarget] = useState(null);
  const [leaving, setLeaving] = useState(false);

  const fetchGroups = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.get('/api/groups/my');
      setGroups(data || []);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchGroups();
  }, [fetchGroups]);

  const handleJoin = async (e) => {
    e.preventDefault();
    const trimmed = code.trim().toUpperCase();
    if (!trimmed) return;
    setJoining(true);
    try {
      const result = await api.post('/api/groups/join', { access_code: trimmed });
      toast.ok(`Joined "${result.group_name}"`);
      setCode('');
      await fetchGroups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setJoining(false);
    }
  };

  const handleLeave = async () => {
    if (!leaveTarget) return;
    setLeaving(true);
    try {
      await api.delete(`/api/groups/${leaveTarget.id}/leave`);
      toast.ok(`Left "${leaveTarget.name}"`);
      setLeaveTarget(null);
      await fetchGroups();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setLeaving(false);
    }
  };

  return (
    <main className="page">
      <PageHeader title="Groups" sub="Join private charger groups, or browse public ones." />

      <section className="card groups-join-card">
        <h2>Join a group</h2>
        <form className="groups-join-form" onSubmit={handleJoin}>
          <div className="field">
            <label className="field-label" htmlFor="groups-access-code">
              Access code
            </label>
            <input
              id="groups-access-code"
              className="input mono"
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="SUNRISE24"
              autoComplete="off"
            />
            <p className="field-help">Codes look like SUNRISE24 — your host shares them.</p>
          </div>
          <button type="submit" className="btn btn-primary" disabled={joining || !code.trim()}>
            {joining ? 'Joining…' : 'Join'}
          </button>
        </form>
      </section>

      <section>
        <h2 className="groups-list-heading">Your groups</h2>

        {loading ? (
          <Skeleton lines={4} />
        ) : error ? (
          <ErrorState error={error} onRetry={fetchGroups} />
        ) : groups.length === 0 ? (
          <EmptyState
            icon={Users}
            title="No groups yet"
            body="Join a private group with an access code, or find public chargers on the map."
            action={
              <Link to="/map" className="btn btn-quiet">
                Browse the map
              </Link>
            }
          />
        ) : (
          <div className="groups-grid">
            {groups.map((group) => (
              <article key={group.id} className="card groups-card">
                <header className="groups-card-header">
                  <h3>{group.name}</h3>
                  <span className={`badge ${group.is_public ? 'badge-ok' : 'badge-info'}`}>
                    {group.is_public ? 'Public' : 'Private'}
                  </span>
                </header>
                <p className="groups-card-meta">
                  {group.plug_count} {group.plug_count === 1 ? 'charger' : 'chargers'}
                </p>
                <div className="groups-card-actions">
                  <Link to={`/?group=${group.id}`} className="btn btn-quiet btn-sm">
                    View chargers
                  </Link>
                  {!group.is_public && (
                    <button
                      type="button"
                      className="btn btn-danger btn-sm"
                      onClick={() => setLeaveTarget(group)}
                    >
                      Leave
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      <ConfirmDialog
        open={Boolean(leaveTarget)}
        onClose={() => setLeaveTarget(null)}
        onConfirm={handleLeave}
        title={`Leave ${leaveTarget?.name || 'this group'}?`}
        body="You'll lose access to its private chargers unless you rejoin with the access code."
        confirmLabel="Leave group"
        tone="danger"
        busy={leaving}
      />
    </main>
  );
}
