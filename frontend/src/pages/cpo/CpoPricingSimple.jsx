/**
 * CpoPricingSimple — the default, "one input" pricing view (owner ask: the
 * full pricing-v2 editor — per-weekday phases, TOD slots, per-group/charger
 * assignment — overwhelms a small CPO who just wants to set one price).
 *
 * Just a "Price per kWh (₹)" field, a plain-language example cost, and a
 * Save button. Saving broadcasts the value across every weekday/time-phase
 * via the *existing* tariff API — it edits (or creates) the tenant's one
 * flat tariff, strips any time-of-day slots off it, and makes it the tenant
 * default (see CpoPricing.jsx `applySimplePrice`). No new backend/schema.
 *
 * Smart-detection summary: `pricingSummary` (from utils/pricingUniformity)
 * tells this component whether the tenant's config is already a single flat
 * rate. When it isn't (different rates by weekday/phase/group/charger are
 * already configured), this view shows a "custom schedule active" banner
 * instead of a misleading single number, and Save routes through a
 * confirmation dialog that spells out the overwrite before it happens.
 */

import { useEffect, useState } from 'react';
import Money from '../../components/ui/Money';
import ConfirmDialog from '../../components/ui/ConfirmDialog';
import { formatINR } from '../../utils/money';

const EXAMPLE_KWH = 3; // "a 3 kW charge for 1 hour" == 3 kWh

const parsePrice = (raw) => {
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
};

export default function CpoPricingSimple({
  pricingSummary,
  seedPrice,
  coinInrRate = 1,
  busy,
  error,
  onSave,
  onSwitchToAdvanced,
}) {
  const [priceInput, setPriceInput] = useState('');
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [localError, setLocalError] = useState('');
  // Once the user has typed, later seedPrice arrivals must not clobber the
  // field: the parent's fetches land in waves, and on a slow connection a
  // late wave was wiping a price typed mid-hydration (also the recurring
  // CpoPricing CI flake). Cleared after a successful save so the parent's
  // refetch re-seeds as before.
  const [dirty, setDirty] = useState(false);

  // Re-seed the field whenever the resolved starting price changes — on
  // first load, and again after a successful save (the parent refetches).
  useEffect(() => {
    if (!dirty) setPriceInput(seedPrice ?? '');
  }, [seedPrice, dirty]);

  if (!pricingSummary) {
    return null; // parent shows a skeleton while this is unresolved
  }

  const price = parsePrice(priceInput);
  const isValid = price != null && price > 0 && price <= 1000;
  const exampleCoins = isValid ? price * EXAMPLE_KWH : null;

  const attemptSave = async () => {
    setLocalError('');
    if (!isValid) {
      setLocalError('Enter a rate greater than ₹0 and at most ₹1000.');
      return;
    }
    if (!pricingSummary.uniform) {
      setConfirmOpen(true);
      return;
    }
    const ok = await onSave(price);
    if (ok) setDirty(false);
  };

  const confirmOverwrite = async () => {
    const ok = await onSave(price);
    if (ok) {
      setConfirmOpen(false);
      setDirty(false);
    }
  };

  return (
    <section className="card pricing-simple stack" aria-label="Simple pricing">
      {pricingSummary.uniform === false && (
        <div className="banner banner-info" role="status">
          <strong>Custom schedule active.</strong> This tenant already has different rates by
          charger, group, or time of day — see Advanced for the full breakdown. Saving here
          replaces all of it with one flat rate.
        </div>
      )}

      <div className="field">
        <label className="field-label" htmlFor="simple-price">
          Price per kWh (₹)
        </label>
        <input
          id="simple-price"
          type="number"
          inputMode="decimal"
          step="0.01"
          min="0.01"
          max="1000"
          className="input"
          value={priceInput}
          onChange={(e) => {
            setDirty(true);
            setPriceInput(e.target.value);
          }}
        />
        <p className="field-help">
          This is the one rate every charger bills at, every hour, every day of the week.
        </p>
      </div>

      <p className="text-2 text-sm pricing-example">
        Example: a 3 kW charge for 1 hour ≈{' '}
        {exampleCoins != null ? (
          <strong>
            <Money coins={exampleCoins} rate={coinInrRate} />
          </strong>
        ) : (
          '—'
        )}
        .
      </p>

      {(localError || error) && (
        <p className="field-error" role="alert">
          {localError || error}
        </p>
      )}

      <div>
        <button type="button" className="btn btn-primary" onClick={attemptSave} disabled={busy}>
          {busy ? 'Saving…' : 'Save price'}
        </button>
      </div>

      <p className="text-3 text-xs">
        Need different rates by time of day, weekday, or charger?{' '}
        <button type="button" className="btn btn-quiet btn-sm" onClick={onSwitchToAdvanced}>
          Switch to Advanced
        </button>
      </p>

      <ConfirmDialog
        open={confirmOpen}
        onClose={() => {
          if (!busy) setConfirmOpen(false);
        }}
        onConfirm={confirmOverwrite}
        title="Replace the custom schedule with a flat rate?"
        body={
          isValid
            ? `This tenant currently bills different rates by charger, group, or time of day. Saving will replace all of that with a single flat rate of ${formatINR(
                price * coinInrRate
              )}/kWh, applied to every charger, every hour.`
            : ''
        }
        confirmLabel="Replace with flat rate"
        tone="danger"
        busy={busy}
      />
    </section>
  );
}
