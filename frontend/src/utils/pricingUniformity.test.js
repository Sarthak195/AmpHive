/**
 * pricingUniformity tests: the Simple/Advanced smart-detection rule in
 * isolation from the CpoPricing page (which is covered separately for the UI
 * side of the same behavior).
 */
import { describe, it, expect } from 'vitest';
import { getPricingSummary } from './pricingUniformity';

const base = {
  groups: [],
  plugs: [],
  defaultTariffId: null,
  slotCounts: {},
  fallbackRate: 5,
};

describe('getPricingSummary', () => {
  it('is uniform at the platform default rate when the tenant has no tariff at all', () => {
    const result = getPricingSummary({ ...base, tariffs: [] });
    expect(result).toEqual({ uniform: true, price: 5, tariffId: null });
  });

  it('is uniform for a single default tariff with no slots and no overrides', () => {
    const result = getPricingSummary({
      ...base,
      tariffs: [{ id: 1, price_per_kwh: 8 }],
      defaultTariffId: 1,
      slotCounts: { 1: 0 },
      groups: [{ tariff_id: null }],
      plugs: [{ tariff_id: null }, { tariff_id: 1 }], // explicit id matching the sole tariff is still uniform
    });
    expect(result).toEqual({ uniform: true, price: 8, tariffId: 1 });
  });

  it('returns null (not yet decidable) while the single tariff\'s slot count is unresolved', () => {
    const result = getPricingSummary({
      ...base,
      tariffs: [{ id: 1, price_per_kwh: 8 }],
      defaultTariffId: 1,
      slotCounts: {}, // id 1 not present yet
    });
    expect(result).toBeNull();
  });

  it('is NOT uniform when the sole tariff carries time-of-day slots', () => {
    const result = getPricingSummary({
      ...base,
      tariffs: [{ id: 1, price_per_kwh: 8 }],
      defaultTariffId: 1,
      slotCounts: { 1: 2 },
    });
    expect(result).toEqual({ uniform: false, price: null, tariffId: 1 });
  });

  it('is NOT uniform when the sole tariff is not the tenant default', () => {
    const result = getPricingSummary({
      ...base,
      tariffs: [{ id: 1, price_per_kwh: 8 }],
      defaultTariffId: null, // nothing set as default
      slotCounts: { 1: 0 },
    });
    expect(result.uniform).toBe(false);
  });

  it('is NOT uniform when a group has a different tariff than the sole tariff', () => {
    const result = getPricingSummary({
      ...base,
      tariffs: [{ id: 1, price_per_kwh: 8 }],
      defaultTariffId: 1,
      slotCounts: { 1: 0 },
      groups: [{ tariff_id: 2 }],
    });
    expect(result.uniform).toBe(false);
  });

  it('is NOT uniform when a plug has a different tariff than the sole tariff', () => {
    const result = getPricingSummary({
      ...base,
      tariffs: [{ id: 1, price_per_kwh: 8 }],
      defaultTariffId: 1,
      slotCounts: { 1: 0 },
      plugs: [{ tariff_id: 99 }],
    });
    expect(result.uniform).toBe(false);
  });

  it('is always NOT uniform with two or more tariffs, even at matching prices, without waiting on slot counts', () => {
    const result = getPricingSummary({
      ...base,
      tariffs: [{ id: 1, price_per_kwh: 8 }, { id: 2, price_per_kwh: 8 }],
      defaultTariffId: 1,
      slotCounts: {}, // deliberately unresolved — must not block a >1-tariff decision
    });
    expect(result).toEqual({ uniform: false, price: null, tariffId: null });
  });
});
