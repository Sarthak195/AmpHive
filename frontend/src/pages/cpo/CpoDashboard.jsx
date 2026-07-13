/**
 * AmpHive CPO Dashboard Page
 * ===========================
 * Main analytics overview for Charge Point Operators.
 * Shows stat cards, revenue/energy charts (Recharts), recent sessions table,
 * and gateway status indicators.
 *
 * Data source: /api/cpo/analytics/overview, /revenue, /energy, /sessions
 */

import { useState, useEffect } from 'react';
import {
  AreaChart, Area, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts';
import CpoLayout from '../../components/CpoLayout';
import CpoAlerts from '../../components/CpoAlerts';
import CpoSummary from '../../components/CpoSummary';
import api from '../../api/client';

/**
 * Custom Recharts tooltip styled to match the glassmorphic dark theme.
 */
const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null;
  return (
    <div style={{
      background: 'hsl(74, 14%, 14%)',
      border: '1px solid hsla(0,0%,100%,0.1)',
      borderRadius: '8px',
      padding: '0.6rem 0.85rem',
      fontSize: '0.8rem',
    }}>
      <p style={{ color: 'var(--color-text-muted)', margin: '0 0 0.25rem' }}>{label}</p>
      {payload.map((entry, i) => (
        <p key={i} style={{ color: entry.color, margin: 0, fontWeight: 600 }}>
          {entry.name}: {typeof entry.value === 'number' ? entry.value.toLocaleString() : entry.value}
        </p>
      ))}
    </div>
  );
};

const CpoDashboard = () => {
  const [overview, setOverview] = useState(null);
  const [revenueData, setRevenueData] = useState([]);
  const [energyData, setEnergyData] = useState([]);
  const [loadData, setLoadData] = useState([]);
  const [recentSessions, setRecentSessions] = useState([]);
  const [tenantName, setTenantName] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const fetchData = async () => {
      try {
        // Fetch all dashboard data in parallel
        const [overviewRes, revenueRes, energyRes, loadRes, sessionsRes, profileRes] = await Promise.all([
          api.get('/api/cpo/analytics/overview'),
          api.get('/api/cpo/analytics/revenue?days=30'),
          api.get('/api/cpo/analytics/energy?days=30'),
          api.get('/api/cpo/analytics/telemetry?days=1&bucket=hour'),
          api.get('/api/cpo/analytics/sessions?limit=10'),
          api.get('/api/cpo/profile'),
        ]);

        setOverview(overviewRes);
        setRevenueData(revenueRes);
        setEnergyData(energyRes);
        setLoadData(loadRes);
        setRecentSessions(sessionsRes);
        setTenantName(profileRes.tenant?.name || '');
      } catch (err) {
        console.error('Failed to load CPO dashboard:', err);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
  }, []);

  /**
   * Format a date string (YYYY-MM-DD) to a shorter display (e.g. "Jun 28").
   */
  const formatDate = (dateStr) => {
    if (!dateStr) return '';
    const d = new Date(dateStr);
    return d.toLocaleDateString('en-IN', { month: 'short', day: 'numeric' });
  };

  /**
   * Format an ISO timestamp to a human-readable datetime.
   */
  const formatDateTime = (isoStr) => {
    if (!isoStr) return '—';
    const d = new Date(isoStr);
    return d.toLocaleString('en-IN', {
      month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  };

  /**
   * Format an ISO timestamp to an hour label (e.g. "14:00") for the load graph.
   */
  const formatHour = (isoStr) => {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', hour12: false });
  };

  /**
   * Get a CSS class for a session status badge.
   */
  const getStatusBadge = (status) => {
    const map = {
      active: 'badge-success',
      completed: 'badge-primary',
      paid: 'badge-primary',
      cancelled: 'badge-danger',
    };
    return map[status] || 'badge-warning';
  };

  if (loading) {
    return (
      <CpoLayout tenantName={tenantName}>
        <div className="cpo-page-header">
          <h1>Dashboard</h1>
          <p>Loading analytics...</p>
        </div>
        <div className="stat-cards">
          {[1, 2, 3, 4].map((i) => (
            <div key={i} className="stat-card">
              <div className="skeleton" style={{ width: '60px', height: '14px', marginBottom: '0.75rem' }} />
              <div className="skeleton" style={{ width: '80px', height: '28px', marginBottom: '0.5rem' }} />
              <div className="skeleton" style={{ width: '100px', height: '12px' }} />
            </div>
          ))}
        </div>
      </CpoLayout>
    );
  }

  return (
    <CpoLayout tenantName={tenantName}>
      {/* Page Header */}
      <div className="cpo-page-header">
        <h1>Dashboard</h1>
        <p>Overview of your charging network performance</p>
      </div>

      {/* Active alerts (safety cutoffs, unauthorized-on, OTA) */}
      <CpoAlerts />

      {/* Stat Cards Row */}
      <div className="stat-cards">
        <div className="stat-card accent-cyan animate-fade-in">
          <div className="stat-card-icon">🔌</div>
          <div className="stat-card-value">{overview?.plugs?.total || 0}</div>
          <div className="stat-card-label">Total Plugs</div>
        </div>

        <div className="stat-card accent-green animate-fade-in animate-delay-1">
          <div className="stat-card-icon">⚡</div>
          <div className="stat-card-value">{overview?.active_sessions || 0}</div>
          <div className="stat-card-label">Active Sessions</div>
        </div>

        <div className="stat-card accent-amber animate-fade-in animate-delay-2">
          <div className="stat-card-icon">🔋</div>
          <div className="stat-card-value">{overview?.today?.energy_kwh || 0}</div>
          <div className="stat-card-label">Today's Energy (kWh)</div>
        </div>

        <div className="stat-card accent-purple animate-fade-in animate-delay-3">
          <div className="stat-card-icon">🪙</div>
          <div className="stat-card-value">{overview?.today?.revenue_coins || 0}</div>
          <div className="stat-card-label">Today's Revenue</div>
        </div>
      </div>

      {/* All-Time Summary */}
      <CpoSummary
        items={[
          { label: 'All-Time Sessions', value: (overview?.all_time?.sessions ?? 0).toLocaleString() },
          { label: 'Total Energy', value: `${(overview?.all_time?.energy_kwh ?? 0).toLocaleString()} kWh` },
          { label: 'Total Revenue', value: `🪙 ${(overview?.all_time?.revenue_coins ?? 0).toLocaleString()}`, tone: 'accent' },
          { label: 'Gateways Online', value: `${overview?.gateways?.online ?? 0} / ${overview?.gateways?.total ?? 0}`, tone: 'success' },
        ]}
      />

      {/* Charts: Revenue and Energy side by side */}
      <div className="charts-grid">
        {/* Revenue Area Chart */}
        <div className="chart-container animate-fade-in">
          <h3>💰 Revenue (Last 30 Days)</h3>
          {revenueData.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={revenueData}>
                <defs>
                  <linearGradient id="revenueGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%" stopColor="hsl(36, 100%, 56%)" stopOpacity={0.3} />
                    <stop offset="95%" stopColor="hsl(36, 100%, 56%)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid strokeDasharray="3 3" stroke="hsla(0,0%,100%,0.06)" />
                <XAxis
                  dataKey="date"
                  tickFormatter={formatDate}
                  stroke="hsla(0,0%,100%,0.2)"
                  tick={{ fill: 'hsl(75, 8%, 50%)', fontSize: 11 }}
                />
                <YAxis
                  stroke="hsla(0,0%,100%,0.2)"
                  tick={{ fill: 'hsl(75, 8%, 50%)', fontSize: 11 }}
                />
                <Tooltip content={<CustomTooltip />} />
                <Area
                  type="monotone"
                  dataKey="revenue_coins"
                  name="Revenue (coins)"
                  stroke="hsl(36, 100%, 56%)"
                  strokeWidth={2}
                  fill="url(#revenueGradient)"
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <div className="empty-state" style={{ padding: '2rem 1rem' }}>
              <p>No revenue data yet</p>
            </div>
          )}
        </div>

        {/* Energy Bar Chart */}
        <div className="chart-container animate-fade-in animate-delay-1">
          <h3>🔋 Energy Consumed (Last 30 Days)</h3>
          {energyData.length > 0 ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={energyData}>
                <CartesianGrid strokeDasharray="3 3" stroke="hsla(0,0%,100%,0.06)" />
                <XAxis
                  dataKey="date"
                  tickFormatter={formatDate}
                  stroke="hsla(0,0%,100%,0.2)"
                  tick={{ fill: 'hsl(75, 8%, 50%)', fontSize: 11 }}
                />
                <YAxis
                  stroke="hsla(0,0%,100%,0.2)"
                  tick={{ fill: 'hsl(75, 8%, 50%)', fontSize: 11 }}
                />
                <Tooltip content={<CustomTooltip />} />
                <Bar
                  dataKey="energy_kwh"
                  name="Energy (kWh)"
                  fill="hsl(73, 100%, 50%)"
                  radius={[4, 4, 0, 0]}
                  maxBarSize={24}
                />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="empty-state" style={{ padding: '2rem 1rem' }}>
              <p>No energy data yet</p>
            </div>
          )}
        </div>
      </div>

      {/* Load Graph — power draw over the last 24h (from persisted telemetry) */}
      <div className="chart-container animate-fade-in animate-delay-2" style={{ marginBottom: '2rem' }}>
        <div className="flex justify-between items-center" style={{ flexWrap: 'wrap', gap: '0.5rem' }}>
          <h3 style={{ margin: 0 }}>⚡ Load (Last 24h)</h3>
          {loadData.length > 0 && (() => {
            const peakW = Math.max(...loadData.map((d) => d.max_power_w || 0));
            const peakA = Math.max(...loadData.map((d) => d.max_current_a || 0));
            return (
              <span style={{ color: 'var(--color-text-muted)', fontSize: '0.82rem' }}>
                Peak <strong style={{ color: 'var(--color-text-secondary)' }}>{peakW.toFixed(0)} W</strong>
                {' · '}<strong style={{ color: 'var(--color-text-secondary)' }}>{peakA.toFixed(1)} A</strong>
              </span>
            );
          })()}
        </div>
        {loadData.length > 0 ? (
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={loadData}>
              <defs>
                <linearGradient id="loadGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="hsl(73, 100%, 50%)" stopOpacity={0.3} />
                  <stop offset="95%" stopColor="hsl(73, 100%, 50%)" stopOpacity={0} />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="hsla(0,0%,100%,0.06)" />
              <XAxis
                dataKey="timestamp"
                tickFormatter={formatHour}
                stroke="hsla(0,0%,100%,0.2)"
                tick={{ fill: 'hsl(75, 8%, 50%)', fontSize: 11 }}
              />
              <YAxis
                stroke="hsla(0,0%,100%,0.2)"
                tick={{ fill: 'hsl(75, 8%, 50%)', fontSize: 11 }}
              />
              <Tooltip content={<CustomTooltip />} labelFormatter={formatHour} />
              <Area
                type="monotone"
                dataKey="avg_power_w"
                name="Avg Power (W)"
                stroke="hsl(73, 100%, 50%)"
                strokeWidth={2}
                fill="url(#loadGradient)"
              />
              <Area
                type="monotone"
                dataKey="max_power_w"
                name="Peak Power (W)"
                stroke="hsl(36, 100%, 56%)"
                strokeWidth={1.5}
                strokeDasharray="4 3"
                fill="none"
              />
            </AreaChart>
          </ResponsiveContainer>
        ) : (
          <div className="empty-state" style={{ padding: '2rem 1rem' }}>
            <p>No telemetry recorded in the last 24h</p>
          </div>
        )}
      </div>

      {/* Recent Sessions Table */}
      <div className="chart-container animate-fade-in animate-delay-2">
        <h3>⚡ Recent Sessions</h3>
        {recentSessions.length > 0 ? (
          <div className="data-table-container" style={{ border: 'none' }}>
            <table className="data-table">
              <thead>
                <tr>
                  <th>Plug</th>
                  <th>User</th>
                  <th>Started</th>
                  <th>Duration</th>
                  <th>Energy</th>
                  <th>Revenue</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {recentSessions.map((session) => (
                  <tr key={session.id}>
                    <td style={{ color: 'var(--color-text-primary)', fontWeight: 500 }}>
                      {session.plug_name}
                    </td>
                    <td>{session.user_email}</td>
                    <td>{formatDateTime(session.started_at)}</td>
                    <td>
                      {session.duration_minutes != null
                        ? `${Math.floor(session.duration_minutes / 60)}h ${Math.round(session.duration_minutes % 60)}m`
                        : '—'
                      }
                    </td>
                    <td>{session.energy_kwh} kWh</td>
                    <td style={{ color: 'var(--color-accent)', fontWeight: 600 }}>
                      🪙 {session.coins_spent}
                    </td>
                    <td>
                      <span className={`badge ${getStatusBadge(session.status)}`}>
                        {session.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="empty-state">
            <div className="empty-state-icon">⚡</div>
            <h3>No sessions yet</h3>
            <p>Sessions will appear here once drivers start charging on your plugs.</p>
          </div>
        )}
      </div>
    </CpoLayout>
  );
};

export default CpoDashboard;
