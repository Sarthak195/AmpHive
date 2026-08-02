/**
 * pricingUniformity — decides whether a tenant's pricing is "uniform" (one
 * flat rate that bills every charger, every hour, the same) or "custom"
 * (different rates by group/charger and/or time of day), and what that one
 * rate is when it IS uniform.
 *
 * This is the smart-detection behind the CPO Pricing page's Simple/Advanced
 * split (see CpoPricing.jsx): Simple mode only makes sense — and only opens
 * by default — when the tenant's config collapses to a single number. A
 * config is "uniform" when ALL of:
 *   - there are 0 tariffs (nothing configured — the platform-wide default
 *     rate applies everywhere), OR
 *   - there is exactly 1 tariff, AND it is the tenant's default tariff, AND
 *     it carries no time-of-day slots, AND no group/charger has been given a
 *     *different* tariff of its own (a null tariff_id inherits, so that's
 *     still uniform).
 * Two or more tariffs is always treated as "custom", even if they happen to
 * share a price today — that's a CPO who has deliberately set up multiple
 * pricing plans, and Simple mode should not paper over that.
 *
 * Pure/no I/O so it's unit-testable without mounting the page.
 */

/**
 * @param {object} args
 * @param {Array<{id:number, price_per_kwh:number|string}>} args.tariffs
 * @param {Array<{tariff_id:number|null}>} args.groups
 * @param {Array<{tariff_id:number|null}>} args.plugs
 * @param {number|null} args.defaultTariffId
 * @param {Record<number, number|null>} args.slotCounts - keyed by tariff id;
 *   a tariff's count must be a settled number (or `null` for "checked, none
 *   known") to decide — `undefined` means "not fetched yet".
 * @param {number} args.fallbackRate - the platform-wide default rate
 *   (coins/kWh) used when the tenant has no tariff at all.
 * @returns {{uniform: boolean, price: number|null, tariffId: number|null} | null}
 *   `null` means "not enough data yet" (still waiting on the per-tariff slot
 *   count) — callers should treat that as "loading", not "custom".
 */
export function getPricingSummary({ tariffs, groups, plugs, defaultTariffId, slotCounts, fallbackRate }) {
  if (!tariffs || tariffs.length === 0) {
    return { uniform: true, price: Number(fallbackRate), tariffId: null };
  }

  if (tariffs.length === 1) {
    const [tariff] = tariffs;
    const slotCount = slotCounts ? slotCounts[tariff.id] : undefined;
    if (slotCount === undefined) return null; // background slot-count fetch hasn't settled yet

    const isDefault = defaultTariffId === tariff.id;
    const groupsInherit = (groups || []).every((g) => g.tariff_id == null || g.tariff_id === tariff.id);
    const plugsInherit = (plugs || []).every((p) => p.tariff_id == null || p.tariff_id === tariff.id);
    const uniform = isDefault && slotCount === 0 && groupsInherit && plugsInherit;

    return {
      uniform,
      price: uniform ? Number(tariff.price_per_kwh) : null,
      tariffId: tariff.id,
    };
  }

  return { uniform: false, price: null, tariffId: null };
}
