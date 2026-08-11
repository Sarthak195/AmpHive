/**
 * AdminFirmwareReleases (feat/ota-version-picker) — the registry an admin
 * maintains so the CPO OTA flow (CpoGateways "Update firmware") offers a
 * dropdown of real versions instead of a hand-pasted URL.
 *
 * Two ways to add a release: "Register release" points at an already-published
 * https URL (docs/FIRMWARE.md §7 / deploy/scripts/publish_firmware.ps1), while
 * "Upload image" streams the raw signed .bin — the backend publishes it to the
 * public bucket gs://amphive-fw (keyless, via the VM service account) and
 * registers the bucket URL. Deactivate/Reactivate soft-toggle a release in/out
 * of the CPO version picker.
 *
 * Data: GET /api/admin/firmware-releases?active=, POST
 * /api/admin/firmware-releases, POST /api/admin/firmware-releases/upload, POST
 * /api/admin/firmware-releases/{id}/deactivate, POST
 * /api/admin/firmware-releases/{id}/reactivate.
 */

import { useCallback, useState } from 'react';
import { UploadCloud, Plus } from 'lucide-react';
import {
  DataTable,
  Modal,
  ConfirmDialog,
  PageHeader,
  useToast,
} from '../../components/ui';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';
import { apiErrorCopy } from '../../utils/statusCopy';

const POLL_MS = 30_000;

export default function AdminFirmwareReleases() {
  const toast = useToast();
  const [releases, setReleases] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [showInactive, setShowInactive] = useState(false);

  const fetchReleases = useCallback(async () => {
    try {
      const res = await api.get(
        showInactive ? '/api/admin/firmware-releases' : '/api/admin/firmware-releases?active=true'
      );
      setReleases(Array.isArray(res) ? res : res?.items || []);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, [showInactive]);

  usePoll(fetchReleases, POLL_MS, [showInactive]);

  // ---- Register a release ------------------------------------------------
  const [registerOpen, setRegisterOpen] = useState(false);
  const [version, setVersion] = useState('');
  const [url, setUrl] = useState('');
  const [notes, setNotes] = useState('');
  const [registerBusy, setRegisterBusy] = useState(false);
  const [registerError, setRegisterError] = useState('');

  const openRegister = () => {
    setVersion('');
    setUrl('');
    setNotes('');
    setRegisterError('');
    setRegisterOpen(true);
  };

  const closeRegister = () => {
    if (registerBusy) return;
    setRegisterOpen(false);
  };

  const submitRegister = async (e) => {
    e.preventDefault();
    setRegisterBusy(true);
    setRegisterError('');
    try {
      await api.post('/api/admin/firmware-releases', {
        version: version.trim(),
        url: url.trim(),
        notes: notes.trim() || undefined,
      });
      setRegisterOpen(false);
      toast.ok(`Registered ${version.trim()}.`);
      fetchReleases();
    } catch (err) {
      setRegisterError(apiErrorCopy(err));
    } finally {
      setRegisterBusy(false);
    }
  };

  // ---- Upload an image (hosts + registers in one step) -------------------
  // Unlike "Register release" (a pointer to an already-published URL), this
  // streams the raw .bin to the backend, which stores it and reads the
  // version out of the ESP32 app image itself.
  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadFile, setUploadFile] = useState(null);
  const [uploadNotes, setUploadNotes] = useState('');
  const [uploading, setUploading] = useState(false);
  const [uploadError, setUploadError] = useState('');

  const openUpload = () => {
    setUploadFile(null);
    setUploadNotes('');
    setUploadError('');
    setUploadOpen(true);
  };

  const closeUpload = () => {
    if (uploading) return;
    setUploadOpen(false);
  };

  const submitUpload = async (e) => {
    e.preventDefault();
    if (!uploadFile) {
      setUploadError('Choose a firmware .bin file to upload.');
      return;
    }
    setUploading(true);
    setUploadError('');
    try {
      const notes = uploadNotes.trim();
      const res = await api.upload(
        `/api/admin/firmware-releases/upload${notes ? `?notes=${encodeURIComponent(notes)}` : ''}`,
        uploadFile
      );
      setUploadOpen(false);
      toast.ok(`Registered ${res?.version ?? 'firmware'}.`);
      fetchReleases();
    } catch (err) {
      setUploadError(apiErrorCopy(err));
    } finally {
      setUploading(false);
    }
  };

  // ---- Deactivate ----------------------------------------------------------
  const [deactivateTarget, setDeactivateTarget] = useState(null);
  const [deactivateBusy, setDeactivateBusy] = useState(false);

  const confirmDeactivate = async () => {
    if (!deactivateTarget) return;
    setDeactivateBusy(true);
    try {
      await api.post(`/api/admin/firmware-releases/${deactivateTarget.id}/deactivate`);
      toast.ok(`${deactivateTarget.version} deactivated.`);
      setDeactivateTarget(null);
      fetchReleases();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setDeactivateBusy(false);
    }
  };

  // ---- Reactivate ----------------------------------------------------------
  const [reactivateTarget, setReactivateTarget] = useState(null);
  const [reactivateBusy, setReactivateBusy] = useState(false);

  const confirmReactivate = async () => {
    if (!reactivateTarget) return;
    setReactivateBusy(true);
    try {
      await api.post(`/api/admin/firmware-releases/${reactivateTarget.id}/reactivate`);
      toast.ok(`${reactivateTarget.version} reactivated.`);
      setReactivateTarget(null);
      fetchReleases();
    } catch (err) {
      toast.error(apiErrorCopy(err));
    } finally {
      setReactivateBusy(false);
    }
  };

  const columns = [
    {
      key: 'version',
      label: 'Version',
      render: (r) => (
        <span className="row">
          <code>{r.version}</code>
          {!r.is_active && <span className="badge">inactive</span>}
        </span>
      ),
    },
    {
      key: 'url',
      label: 'Image URL',
      render: (r) => (
        <span className="text-2 text-sm" style={{ wordBreak: 'break-all' }}>
          {r.url}
        </span>
      ),
    },
    { key: 'notes', label: 'Notes', render: (r) => r.notes || '—' },
    { key: 'created_at', label: 'Registered', render: (r) => (r.created_at ? new Date(r.created_at).toLocaleDateString() : '—') },
    {
      key: 'actions',
      label: 'Actions',
      render: (r) =>
        r.is_active ? (
          <button
            type="button"
            className="btn btn-quiet btn-sm"
            onClick={() => setDeactivateTarget(r)}
          >
            Deactivate
          </button>
        ) : (
          <button
            type="button"
            className="btn btn-quiet btn-sm"
            onClick={() => setReactivateTarget(r)}
          >
            Reactivate
          </button>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Firmware releases"
        sub="Registered OTA images the CPO version picker draws from — see docs/FIRMWARE.md for how images are published."
        actions={
          <>
            <button type="button" className="btn btn-quiet" onClick={openUpload}>
              <UploadCloud size={16} aria-hidden="true" />
              Upload image
            </button>
            <button type="button" className="btn btn-primary" onClick={openRegister}>
              <Plus size={16} aria-hidden="true" />
              Register release
            </button>
          </>
        }
      />

      <div className="filter-bar">
        <label className="row text-sm">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={(e) => setShowInactive(e.target.checked)}
          />
          Show deactivated releases
        </label>
      </div>

      <DataTable
        columns={columns}
        rows={releases}
        loading={loading}
        error={error}
        onRetry={fetchReleases}
        emptyIcon={UploadCloud}
        emptyTitle="No firmware releases yet"
        emptyBody="Publish an image (docs/FIRMWARE.md §7), then register its version and URL here."
        emptyAction={
          <button type="button" className="btn btn-primary btn-sm" onClick={openRegister}>
            Register release
          </button>
        }
        collapse
      />

      <Modal open={registerOpen} onClose={closeRegister} title="Register a firmware release">
        <form className="stack" onSubmit={submitRegister}>
          <p className="text-2 text-sm">
            This registers a version pointer for the CPO picker — it does not
            upload a binary. Publish the image first (<code>gcloud storage cp</code> to{' '}
            <code>gs://amphive-fw</code>, or <code>deploy/scripts/publish_firmware.ps1</code>),
            then paste its public URL below.
          </p>
          <div className="field">
            <label className="field-label" htmlFor="fw-version">
              Version
            </label>
            <input
              id="fw-version"
              className="input"
              value={version}
              onChange={(e) => setVersion(e.target.value)}
              placeholder="2.4.0-direct"
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="fw-url">
              Image URL (https)
            </label>
            <input
              id="fw-url"
              type="url"
              className="input"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://storage.googleapis.com/amphive-fw/amphive-gateway-2.4.0.bin"
              pattern="https://.*"
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="fw-notes">
              Notes <span className="text-2 text-sm">(optional)</span>
            </label>
            <input
              id="fw-notes"
              className="input"
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Adds sub-16A on-device current cap enforcement"
            />
          </div>
          {registerError && <p className="field-error">{registerError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={closeRegister} disabled={registerBusy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={registerBusy}>
              {registerBusy ? 'Registering…' : 'Register'}
            </button>
          </div>
        </form>
      </Modal>

      <Modal open={uploadOpen} onClose={closeUpload} title="Upload firmware image">
        <form className="stack" onSubmit={submitUpload}>
          <p className="text-2 text-sm">
            Signed build output (<code>build/amphive-gateway.bin</code>) — version is read from
            the image itself. It&apos;s published to <code>gs://amphive-fw</code> and registered
            in one step, so there&apos;s no URL to paste.
          </p>
          <div className="field">
            <label className="field-label" htmlFor="fw-upload-file">
              Firmware image (.bin)
            </label>
            <input
              id="fw-upload-file"
              className="input"
              type="file"
              accept=".bin,application/octet-stream"
              onChange={(e) => setUploadFile(e.target.files?.[0] || null)}
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="fw-upload-notes">
              Notes <span className="text-2 text-sm">(optional)</span>
            </label>
            <input
              id="fw-upload-notes"
              className="input"
              value={uploadNotes}
              onChange={(e) => setUploadNotes(e.target.value)}
              placeholder="Adds sub-16A on-device current cap enforcement"
            />
          </div>
          {uploadError && <p className="field-error">{uploadError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={closeUpload} disabled={uploading}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={uploading || !uploadFile}>
              {uploading ? 'Uploading…' : 'Upload image'}
            </button>
          </div>
        </form>
      </Modal>

      <ConfirmDialog
        open={Boolean(deactivateTarget)}
        onClose={() => setDeactivateTarget(null)}
        onConfirm={confirmDeactivate}
        title="Deactivate this release?"
        body={`${deactivateTarget?.version || 'This release'} will disappear from the CPO version picker immediately. It stays here for reference and can be reactivated later from this page.`}
        confirmLabel="Deactivate"
        tone="danger"
        busy={deactivateBusy}
        busyLabel="Deactivating…"
      />

      <ConfirmDialog
        open={Boolean(reactivateTarget)}
        onClose={() => setReactivateTarget(null)}
        onConfirm={confirmReactivate}
        title="Reactivate this release?"
        body={`${reactivateTarget?.version || 'This release'} will reappear in the CPO version picker immediately.`}
        confirmLabel="Reactivate"
        tone="primary"
        busy={reactivateBusy}
        busyLabel="Reactivating…"
      />
    </>
  );
}
