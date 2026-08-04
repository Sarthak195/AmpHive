/**
 * AdminOverview — platform-wide KPI grid for the admin console.
 * ==============================================================
 * Every tile is a link into the admin page that can explain the number
 * (tenants, users, gateways, payouts, disputes); revenue/energy/sessions have
 * no dedicated per-metric page so they link into Tenants, the finest-grained
 * cross-tenant breakdown available today.
 *
 * Data: GET /api/admin/stats/overview, polled every 30s while the tab is
 * visible (backend may lag — always ErrorState-with-retry, never a blank
 * or silently-stale grid on failure).
 */

import { useCallback, useState } from 'react';
import { Link } from 'react-router-dom';
import { Banknote, Zap, BatteryCharging, Building2, Users, Radio, PlugZap, Wallet, Scale } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import ErrorState from '../../components/ui/ErrorState';
import Skeleton, { SkeletonTitle } from '../../components/ui/Skeleton';
import Money from '../../components/ui/Money';
import api from '../../api/client';
import usePoll from '../../hooks/usePoll';
import { useConfig } from '../../contexts/ConfigContext';
import { formatKwh } from '../../utils/money';
import './AdminOverview.css';

const POLL_MS = 30_000;
const SKELETON_TILES = 11;

const AdminOverview = () => {
  const { coin_inr_rate: rate } = useConfig();
  const [stats, setStats] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    try {
      const data = await api.get('/api/admin/stats/overview');
      setStats(data);
      setError(null);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  usePoll(load, POLL_MS);

  if (loading) {
    return (
      <>
        <PageHeader eyebrow="Platform" title="Overview" sub="How AmpHive is doing right now." />
        <p className="sr-only" role="status">Loading platform overview…</p>
        <div className="kpi-grid" aria-hidden="true">
          {Array.from({ length: SKELETON_TILES }, (_, i) => (
            <div className="kpi" key={i}>
              <SkeletonTitle />
              <Skeleton lines={1} />
            </div>
          ))}
        </div>
      </>
    );
  }

  if (error) {
    return (
      <>
        <PageHeader eyebrow="Platform" title="Overview" sub="How AmpHive is doing right now." />
        <ErrorState error={error} onRetry={load} title="Couldn't load platform stats" />
      </>
    );
  }

  const tiles = [
    {
      key: 'revenue',
      to: '/admin/tenants',
      icon: Banknote,
      label: 'Revenue today',
      value: <Money coins={stats.revenue_coins?.today} rate={rate} />,
      sub: <>All-time <Money coins={stats.revenue_coins?.total} rate={rate} /></>,
    },
    {
      key: 'revenue_30d',
      to: '/admin/tenants',
      icon: Banknote,
      label: 'Revenue (30d)',
      value: <Money coins={stats.revenue_coins?.last_30d ?? 0} rate={rate} />,
      sub: 'Last 30 days',
    },
    {
      key: 'energy',
      to: '/admin/tenants',
      icon: Zap,
      label: 'Energy today',
      value: formatKwh(stats.energy_kwh?.today),
      sub: `All-time ${formatKwh(stats.energy_kwh?.total)}`,
    },
    {
      key: 'energy_30d',
      to: '/admin/tenants',
      icon: Zap,
      label: 'Energy (30d)',
      value: formatKwh(stats.energy_kwh?.last_30d ?? 0),
      sub: 'Last 30 days',
    },
    {
      key: 'sessions',
      to: '/admin/tenants',
      icon: BatteryCharging,
      label: 'Active sessions',
      value: stats.sessions?.active ?? 0,
      sub: `${stats.sessions?.today ?? 0} today · ${stats.sessions?.total ?? 0} all-time`,
    },
    {
      key: 'tenants',
      to: '/admin/tenants',
      icon: Building2,
      label: 'Tenants',
      value: stats.tenants ?? 0,
      sub: 'Organizations on the platform',
    },
    {
      key: 'users',
      to: '/admin/users',
      icon: Users,
      label: 'Users',
      value: stats.users?.total ?? 0,
      sub: `${stats.users?.drivers ?? 0} drivers · ${stats.users?.cpos ?? 0} operators · ${stats.users?.admins ?? 0} admins`,
    },
    {
      key: 'gateways',
      to: '/admin/gateways',
      icon: Radio,
      label: 'Gateways online',
      value: `${stats.gateways?.online ?? 0} / ${stats.gateways?.total ?? 0}`,
      sub: 'Reporting across the fleet',
    },
    {
      key: 'chargers',
      to: '/admin/chargers',
      icon: PlugZap,
      label: 'Chargers',
      value: stats.plugs?.total ?? 0,
      sub: `${stats.plugs?.public ?? 0} public · ${stats.plugs?.private ?? 0} private`,
    },
    {
      key: 'payouts',
      to: '/admin/payouts',
      icon: Wallet,
      label: 'Requested payouts',
      value: <Money coins={stats.payouts?.requested_net_coins} rate={rate} />,
      sub: `${stats.payouts?.requested_count ?? 0} awaiting transfer`,
    },
    {
      key: 'disputes',
      to: '/admin/disputes',
      icon: Scale,
      label: 'Open disputes',
      value: stats.disputes?.open ?? 0,
      sub: 'Awaiting CPO resolution',
    },
  ];

  return (
    <>
      <PageHeader eyebrow="Platform" title="Overview" sub="How AmpHive is doing right now." />
      <div className="kpi-grid">
        {tiles.map(({ key, to, icon: Icon, label, value, sub }) => (
          <Link key={key} to={to} className="kpi">
            <span className="kpi-label admin-kpi-label">
              <Icon size={14} aria-hidden="true" />
              {label}
            </span>
            <span className="kpi-value">{value}</span>
            {sub && <span className="kpi-sub">{sub}</span>}
          </Link>
        ))}
      </div>
    </>
  );
};

export default AdminOverview;
