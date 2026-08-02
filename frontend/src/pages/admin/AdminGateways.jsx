/**
 * AdminGateways — fleet view of all gateways across tenants.
 *
 * Shows a paginated table of gateways with organization, name, online/offline
 * status, firmware version (with behind-latest badge), and plug count. Includes
 * an online/offline filter and a firmware-spread summary showing how many
 * gateways run each version.
 *
 * Also mints preflashed-unit inventory (claim-code onboarding,
 * feat/easy-provisioning): "Mint inventory gateway" POSTs
 * /api/admin/gateways/inventory with the device's MAC, then shows the
 * freshly generated claim code once so the operator can copy it onto the
 * unit's label (see deploy/docs/preflashed_unit_runbook.md — this is the
 * "mint inventory row" step, done after flashing + before creating the
 * broker account). This fleet table only ever shows CLAIMED gateways
 * (GET /api/admin/gateways inner-joins Tenant); unclaimed inventory has no
 * tenant to join and doesn't belong in a tenant fleet view.
 *
 * Data source: GET /api/admin/gateways?online=&limit=&offset=
 */

import { useState, useCallback } from 'react';
import { Server, Plus } from 'lucide-react';
import {
  DataTable,
  ErrorState,
  Modal,
  PageHeader,
  StatusDot,
  useToast,
} from '../../components/ui';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';
import { apiErrorCopy } from '../../utils/statusCopy';

const DEFAULT_LIMIT = 25;

/**
 * Compute the maximum firmware version in a list (semantic versioning).
 * Returns null if no versions are present.
 */
function maxVersion(gateways) {
  const versions = gateways
    .map((g) => g.firmware_version)
    .filter((v) => v && v.trim());

  if (!versions.length) return null;

  return versions.sort((a, b) => {
    const aParts = a.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
    const bParts = b.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
    for (let i = 0; i < Math.max(aParts.length, bParts.length); i++) {
      const av = aParts[i] || 0;
      const bv = bParts[i] || 0;
      if (av !== bv) return bv - av; // descending: biggest first
    }
    return 0;
  })[0];
}

/**
 * Compute a map of { firmware_version: count } for the summary chips.
 */
function firmwareSpread(gateways) {
  const counts = {};
  gateways.forEach((g) => {
    if (g.firmware_version) {
      counts[g.firmware_version] = (counts[g.firmware_version] || 0) + 1;
    }
  });
  return counts;
}

/**
 * Format relative time (e.g. "2h ago", "3d ago", "never").
 */
function relativeTime(iso) {
  if (!iso) return 'never';
  const now = new Date();
  const then = new Date(iso);
  const secs = Math.floor((now - then) / 1000);

  if (secs < 0) return 'just now';
  if (secs < 60) return `${secs}s ago`;
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}

export default function AdminGateways() {
  const toast = useToast();
  const [data, setData] = useState({
    items: [],
    total: 0,
  });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // Filters
  const [onlineFilter, setOnlineFilter] = useState('all'); // 'all' | 'online' | 'offline'
  const [offset, setOffset] = useState(0);

  // ---- Mint inventory gateway (preflashed-unit claim-code onboarding) --
  const [mintOpen, setMintOpen] = useState(false);
  const [mintGatewayId, setMintGatewayId] = useState('');
  const [mintName, setMintName] = useState('');
  const [mintBusy, setMintBusy] = useState(false);
  const [mintError, setMintError] = useState('');
  const [mintResult, setMintResult] = useState(null); // { gateway_id, claim_code } once minted
  const [codeCopied, setCodeCopied] = useState(false);

  const openMint = () => {
    setMintGatewayId('');
    setMintName('');
    setMintError('');
    setMintResult(null);
    setCodeCopied(false);
    setMintOpen(true);
  };

  const closeMint = () => {
    if (mintBusy) return;
    setMintOpen(false);
  };

  const submitMint = async (e) => {
    e.preventDefault();
    setMintBusy(true);
    setMintError('');
    try {
      const res = await api.post('/api/admin/gateways/inventory', {
        gateway_id: mintGatewayId.trim(),
        name: mintName.trim() || undefined,
      });
      setMintResult(res);
    } catch (err) {
      setMintError(apiErrorCopy(err));
    } finally {
      setMintBusy(false);
    }
  };

  const copyClaimCode = async () => {
    if (!mintResult?.claim_code) return;
    try {
      await navigator.clipboard.writeText(mintResult.claim_code);
      setCodeCopied(true);
      toast.ok('Claim code copied.');
      setTimeout(() => setCodeCopied(false), 2000);
    } catch {
      toast.error('Could not copy — copy the code manually.');
    }
  };

  const fetchGateways = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams();
      params.append('limit', DEFAULT_LIMIT);
      params.append('offset', offset);

      // Filter by online status if not 'all'
      if (onlineFilter !== 'all') {
        params.append('online', onlineFilter === 'online' ? 'true' : 'false');
      }

      const res = await api.get(`/api/admin/gateways?${params}`);
      setData(res || { items: [], total: 0 });
    } catch (err) {
      setError(err);
      setData({ items: [], total: 0 });
    } finally {
      setLoading(false);
    }
  }, [offset, onlineFilter]);

  // Poll every 30s to catch online/offline state and firmware changes
  usePoll(fetchGateways, 30_000, [offset, onlineFilter]);

  const items = data.items || [];
  const total = data.total || 0;

  // Compute firmware spread summary
  const spread = firmwareSpread(items);
  const maxFw = maxVersion(items);
  const spreadChips = Object.entries(spread)
    .sort(([v1], [v2]) => {
      const v1Parts = v1.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
      const v2Parts = v2.split(/[.-]/).map((p) => parseInt(p, 10) || 0);
      for (let i = 0; i < Math.max(v1Parts.length, v2Parts.length); i++) {
        const av = v1Parts[i] || 0;
        const bv = v2Parts[i] || 0;
        if (av !== bv) return bv - av;
      }
      return 0;
    })
    .map(([version, count]) => ({
      version,
      count,
    }));

  // DataTable columns
  const columns = [
    { key: 'tenant_name', label: 'Organization' },
    { key: 'name', label: 'Gateway' },
    { key: 'last_seen', label: 'Status' },
    { key: 'firmware_version', label: 'Firmware' },
    { key: 'plug_count', label: 'Chargers', num: true },
  ];

  // Render status + last seen in one column
  const rows = items.map((gw) => ({
    ...gw,
    last_seen: (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <StatusDot state={gw.online ? 'available' : 'offline'} />
        <span>{relativeTime(gw.last_seen_at)}</span>
      </div>
    ),
    firmware_version: (
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
        <code>{gw.firmware_version || '—'}</code>
        {maxFw && gw.firmware_version && gw.firmware_version !== maxFw && (
          <span className="badge badge-warn">behind</span>
        )}
      </div>
    ),
  }));

  const mintAction = (
    <button type="button" className="btn btn-primary" onClick={openMint}>
      <Plus size={16} aria-hidden="true" />
      Mint inventory gateway
    </button>
  );

  const mintModal = (
    <Modal open={mintOpen} onClose={closeMint} title="Mint inventory gateway">
      {mintResult ? (
        <div className="stack">
          <p className="text-2 text-sm">
            Gateway <code>{mintResult.gateway_id}</code> is now unclaimed inventory. Copy this
            claim code onto the unit&apos;s label — it&apos;s shown here once.
          </p>
          <div className="field">
            <label className="field-label" htmlFor="mint-claim-code">
              Claim code
            </label>
            <div style={{ display: 'flex', gap: '8px' }}>
              <input id="mint-claim-code" className="input" value={mintResult.claim_code} readOnly />
              <button type="button" className="btn btn-quiet" onClick={copyClaimCode}>
                {codeCopied ? 'Copied!' : 'Copy'}
              </button>
            </div>
          </div>
          <div className="modal-actions">
            <button type="button" className="btn btn-primary" onClick={closeMint}>
              Done
            </button>
          </div>
        </div>
      ) : (
        <form className="stack" onSubmit={submitMint}>
          <p className="text-2 text-sm">
            Pre-register a preflashed unit as unclaimed inventory with a fresh claim code (deploy/docs/preflashed_unit_runbook.md).
            The gateway ID is the device&apos;s MAC, read off it during flashing.
          </p>
          <div className="field">
            <label className="field-label" htmlFor="mint-gateway-id">
              Gateway ID (device MAC)
            </label>
            <input
              id="mint-gateway-id"
              className="input"
              value={mintGatewayId}
              onChange={(e) => setMintGatewayId(e.target.value)}
              placeholder="a1b2c3d4e5f6"
              required
            />
          </div>
          <div className="field">
            <label className="field-label" htmlFor="mint-name">
              Name <span className="text-2 text-sm">(optional)</span>
            </label>
            <input
              id="mint-name"
              className="input"
              value={mintName}
              onChange={(e) => setMintName(e.target.value)}
              placeholder="Batch 2026-08 unit 014"
            />
          </div>
          {mintError && <p className="field-error">{mintError}</p>}
          <div className="modal-actions">
            <button type="button" className="btn btn-quiet" onClick={closeMint} disabled={mintBusy}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={mintBusy}>
              {mintBusy ? 'Minting…' : 'Mint gateway'}
            </button>
          </div>
        </form>
      )}
    </Modal>
  );

  if (error) {
    return (
      <>
        <PageHeader title="Gateways" sub="Fleet overview and status" actions={mintAction} />
        <ErrorState error={error} onRetry={fetchGateways} title="Couldn't load gateways" />
        {mintModal}
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Gateways"
        sub={`${total} gateway${total !== 1 ? 's' : ''} across the network`}
        actions={mintAction}
      />

      {/* Firmware spread summary */}
      {!loading && items.length > 0 && (
        <div className="filter-bar">
          <div className="filter-count">
            {spreadChips.length > 0 && (
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                {spreadChips.map(({ version, count }) => (
                  <span key={version} className="chip">
                    {version} ×{count}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Online/Offline filter */}
      <div className="filter-bar">
        <div style={{ display: 'flex', gap: '8px', alignItems: 'center' }}>
          <label htmlFor="online-filter" className="field-label">
            Status:
          </label>
          <select
            id="online-filter"
            className="select"
            value={onlineFilter}
            onChange={(e) => {
              setOnlineFilter(e.target.value);
              setOffset(0); // reset pagination on filter change
            }}
          >
            <option value="all">All</option>
            <option value="online">Online only</option>
            <option value="offline">Offline only</option>
          </select>
        </div>
      </div>

      {/* DataTable */}
      <div className="table-wrap">
        <DataTable
          columns={columns}
          rows={rows}
          keyField="id"
          loading={loading}
          emptyIcon={Server}
          emptyTitle="No gateways yet"
          emptyBody="Gateways will appear here once registered."
          pagination={{
            total,
            offset,
            limit: DEFAULT_LIMIT,
            onPage: (newOffset) => {
              setOffset(newOffset);
            },
          }}
        />
      </div>
      {mintModal}
    </>
  );
}
