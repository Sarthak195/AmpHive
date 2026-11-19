/**
 * CpoPlugPhotos: moderation queue for driver-submitted charger photos
 * (backend/database/models.py PlugPhoto; driver upload in
 * backend/routers/plugs.py: POST /api/plugs/{id}/photos). A photo lands PENDING
 * and is invisible on the public map until approved here.
 *
 * Data: GET /api/cpo/plug-photos[?status_filter=pending|approved|rejected]
 * (tenant-scoped, newest first). Plug name isn't on that shape, so this page
 * enriches best-effort from GET /api/cpo/plugs (same as CpoPlugReports).
 *
 * Mutation: POST /api/cpo/plug-photos/{id}/approve | /reject (reject also
 * deletes the stored bytes server-side, and works on approved photos too).
 *
 * Images: p.url is the backend image route, which serves PENDING bytes only to
 * a moderator, so thumbnails are fetched with the Bearer token (an <img> can't
 * send it). Rejected photos have no bytes left; they show a placeholder.
 */
import { useCallback, useEffect, useState } from 'react';
import { Image as ImageIcon } from 'lucide-react';
import CpoLayout from '../../components/CpoLayout';
import { PageHeader, DataTable, Tabs, useToast } from '../../components/ui';
import api from '../../api/client';
import { apiErrorCopy } from '../../utils/statusCopy';
import './CpoPlugPhotos.css';

const LIMIT = 200;
const API_BASE = import.meta.env.VITE_API_URL || '';

function PhotoThumb({ photo }) {
  const [src, setSrc] = useState(null);
  const removed = photo.status === 'rejected';

  useEffect(() => {
    if (removed) return undefined;
    let objectUrl = null;
    let cancelled = false;
    const token = localStorage.getItem('amphive_token');
    fetch(`${API_BASE}${photo.url}`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    })
      .then((res) => (res.ok ? res.blob() : null))
      .then((blob) => {
        if (!blob || cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setSrc(objectUrl);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [photo.url, removed]);

  if (removed || !src) {
    return (
      <span className="cpo-photo-thumb is-empty">
        <ImageIcon size={16} aria-hidden="true" />
        {removed ? 'Removed' : null}
      </span>
    );
  }
  return (
    <a href={src} target="_blank" rel="noopener noreferrer" className="cpo-photo-thumb">
      <img src={src} alt={`Charger #${photo.plug_id}`} />
    </a>
  );
}

const STATUS_BADGE = {
  pending: 'badge-warn',
  approved: 'badge-ok',
  rejected: 'badge-info',
};

const TABS = [
  { id: 'pending', label: 'Pending' },
  { id: 'approved', label: 'Approved' },
  { id: 'rejected', label: 'Rejected' },
  { id: '', label: 'All' },
];

const formatDateTime = (iso) =>
  iso
    ? new Date(iso).toLocaleString('en-IN', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    : '-';

export default function CpoPlugPhotos() {
  const toast = useToast();

  const [statusFilter, setStatusFilter] = useState('pending');
  const [photos, setPhotos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);

  const fetchPhotos = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.set('limit', String(LIMIT));
      if (statusFilter) params.set('status_filter', statusFilter);
      const res = await api.get(`/api/cpo/plug-photos?${params.toString()}`);
      setPhotos(Array.isArray(res) ? res : res?.items || []);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [statusFilter]);

  useEffect(() => {
    fetchPhotos();
  }, [fetchPhotos]);

  // ---- Best-effort plug-name enrichment (same as CpoPlugReports) -----------
  const [plugMap, setPlugMap] = useState({});
  useEffect(() => {
    let cancelled = false;
    api
      .get('/api/cpo/plugs')
      .then((res) => {
        if (cancelled) return;
        const items = Array.isArray(res) ? res : res?.items || [];
        setPlugMap(Object.fromEntries(items.map((p) => [p.id, p])));
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, []);

  const moderate = async (photo, action) => {
    setBusyId(photo.id);
    try {
      await api.post(`/api/cpo/plug-photos/${photo.id}/${action}`);
      toast.ok(action === 'approve' ? 'Photo approved.' : 'Photo rejected and removed.');
      await fetchPhotos();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setBusyId(null);
    }
  };

  const columns = [
    {
      key: 'url',
      label: 'Photo',
      render: (p) => <PhotoThumb photo={p} />,
    },
    {
      key: 'plug_id',
      label: 'Charger',
      render: (p) => plugMap[p.plug_id]?.name || `Charger #${p.plug_id}`,
    },
    {
      key: 'status',
      label: 'Status',
      render: (p) => (
        <span className={`badge ${STATUS_BADGE[p.status] || 'badge-info'}`}>{p.status}</span>
      ),
    },
    { key: 'created_at', label: 'Submitted', render: (p) => formatDateTime(p.created_at) },
    {
      key: 'actions',
      label: 'Actions',
      render: (p) =>
        p.status === 'rejected' ? (
          <span aria-hidden="true">-</span>
        ) : (
          <span className="row cpo-photo-actions">
            {p.status === 'pending' && (
              <button data-write
                type="button"
                className="btn btn-primary btn-sm"
                disabled={busyId === p.id}
                onClick={() => moderate(p, 'approve')}
              >
                Approve
              </button>
            )}
            <button data-write
              type="button"
              className="btn btn-quiet btn-sm"
              disabled={busyId === p.id}
              onClick={() => moderate(p, 'reject')}
            >
              Reject
            </button>
          </span>
        ),
    },
  ];

  return (
    <CpoLayout>
      <PageHeader
        title="Plug photos"
        sub="Approve or reject photos drivers have submitted for your chargers before they show on the public map."
      />

      <Tabs
        tabs={TABS}
        active={statusFilter}
        onChange={setStatusFilter}
        ariaLabel="Filter plug photos by status"
      >
        <DataTable
          columns={columns}
          rows={photos}
          loading={loading}
          error={error}
          onRetry={fetchPhotos}
          emptyIcon={ImageIcon}
          emptyTitle={statusFilter === 'pending' ? 'Nothing to review' : 'No photos'}
          emptyBody={
            statusFilter === 'pending'
              ? 'No photos are waiting for approval right now.'
              : 'Driver-submitted charger photos show up here.'
          }
          collapse
        />
      </Tabs>
    </CpoLayout>
  );
}
