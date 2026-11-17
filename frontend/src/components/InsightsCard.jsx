/**
 * InsightsCard: the driver's 12-month charging trend plus ESTIMATED CO2 and
 * petrol figures, drawn as inline SVG bars (no chart library, so the driver
 * first-load bundle never pulls in recharts). Lazy-loaded by the Dashboard.
 *
 * Reads the `series` and `impact` blocks of GET /api/me/stats. Every derived
 * figure is labelled an estimate and the factors used are shown beneath.
 */

import { coinsToINR, formatINR, formatKwh } from '../utils/money';
import './InsightsCard.css';

const W = 300;
const H = 90;
const BAR_GAP = 4;
const MONTH_LETTERS = 'JFMAMJJASOND';

const monthLetter = (key) => MONTH_LETTERS[Number(key.slice(5, 7)) - 1] ?? '';
const monthLabel = (key) =>
  new Date(`${key}-01T00:00:00Z`).toLocaleDateString('en-IN', {
    month: 'short', year: 'numeric', timeZone: 'UTC',
  });

const InsightsCard = ({ series, impact, rate = 1 }) => {
  if (!Array.isArray(series) || series.length === 0) return null;

  const maxKwh = Math.max(...series.map((m) => m.energy_kwh || 0), 0);
  const barW = (W - BAR_GAP * (series.length - 1)) / series.length;
  const totals = series.reduce(
    (t, m) => ({
      kwh: t.kwh + (m.energy_kwh || 0),
      coins: t.coins + (m.spend_coins || 0),
      sessions: t.sessions + (m.sessions || 0),
    }),
    { kwh: 0, coins: 0, sessions: 0 },
  );
  const life = impact?.lifetime;
  const a = impact?.assumptions;

  return (
    <section className="card insights" aria-label="Charging insights">
      <h2 className="insights-title">Insights</h2>
      <p className="text-3 text-sm">
        Last 12 months: <span className="num">{formatKwh(totals.kwh)}</span>
        {' · '}
        <span className="num">{formatINR(coinsToINR(totals.coins, rate))}</span>
        {' · '}
        <span className="num">{totals.sessions}</span> sessions
      </p>

      <svg
        className="insights-bars"
        viewBox={`0 0 ${W} ${H + 14}`}
        role="img"
        aria-label={`Energy charged per month, last ${series.length} months`}
      >
        {series.map((m, i) => {
          const h = maxKwh > 0 ? Math.max((m.energy_kwh / maxKwh) * H, m.energy_kwh > 0 ? 2 : 0) : 0;
          const x = i * (barW + BAR_GAP);
          return (
            <g key={m.month}>
              <rect className="insights-bar" x={x} y={H - h} width={barW} height={h} rx="2">
                <title>
                  {`${monthLabel(m.month)}: ${formatKwh(m.energy_kwh)}, ${formatINR(coinsToINR(m.spend_coins, rate))}, ${m.sessions} sessions`}
                </title>
              </rect>
              <text className="insights-tick" x={x + barW / 2} y={H + 11} textAnchor="middle">
                {monthLetter(m.month)}
              </text>
            </g>
          );
        })}
      </svg>

      {life && a && (
        <>
          <dl className="insights-est">
            {life.co2_avoided_kg > 0 && (
              <div>
                <dt className="text-3 text-xs">CO₂ avoided, all time (estimate)</dt>
                <dd className="num">≈ {life.co2_avoided_kg.toFixed(1)} kg</dd>
              </div>
            )}
            <div>
              <dt className="text-3 text-xs">Petrol not burned (estimate)</dt>
              <dd className="num">
                ≈ {life.petrol_litres.toFixed(1)} L ({formatINR(life.petrol_equivalent_inr)})
              </dd>
            </div>
          </dl>
          <p className="insights-note text-3 text-xs">
            Estimates, not measurements. Assumes {a.ev_km_per_kwh} km per kWh against a petrol
            vehicle doing {a.ice_km_per_l} km/L ({a.petrol_co2_kg_per_l} kg CO₂ per litre, ₹
            {a.petrol_price_per_l}/L), less grid CO₂ at {a.grid_emission_factor_kg_per_kwh} kg/kWh.
            Months are UTC.
          </p>
        </>
      )}
    </section>
  );
};

export default InsightsCard;
