/**
 * money.js tests: en-IN ₹ formatting (grouping, decimals, negatives, null),
 * coin→₹ conversion, and the kWh / kW / duration formatters.
 */
import { describe, it, expect } from 'vitest';

import { formatINR, coinsToINR, formatKwh, formatKw, formatDuration } from './money';

describe('formatINR', () => {
  it('formats with two decimals and en-IN grouping', () => {
    expect(formatINR(1240.5)).toBe('₹1,240.50');
    expect(formatINR(124050.5)).toBe('₹1,24,050.50');
    expect(formatINR(0)).toBe('₹0.00');
    expect(formatINR(6.174)).toBe('₹6.17');
  });

  it('keeps the sign ahead of the rupee mark', () => {
    expect(formatINR(-50)).toBe('-₹50.00');
  });

  it('renders a dash for null/undefined/NaN', () => {
    expect(formatINR(null)).toBe('—');
    expect(formatINR(undefined)).toBe('—');
    expect(formatINR('not a number')).toBe('—');
  });
});

describe('coinsToINR', () => {
  it('converts at the given rate (default 1:1)', () => {
    expect(coinsToINR(100)).toBe(100);
    expect(coinsToINR(100, 1.5)).toBe(150);
    expect(coinsToINR('12.5', 2)).toBe(25);
  });

  it('returns 0 for unusable input', () => {
    expect(coinsToINR(undefined)).toBe(0);
    expect(coinsToINR('x', 1)).toBe(0);
  });
});

describe('formatKwh / formatKw', () => {
  it('formats energy with two decimals', () => {
    expect(formatKwh(1.234)).toBe('1.23 kWh');
    expect(formatKwh(0)).toBe('0.00 kWh');
    expect(formatKwh(null)).toBe('—');
  });

  it('formats watts, switching to kW at 1000', () => {
    expect(formatKw(640)).toBe('640 W');
    expect(formatKw(2300)).toBe('2.3 kW');
    expect(formatKw(10000)).toBe('10.0 kW');
    expect(formatKw(null)).toBe('—');
  });
});

describe('formatDuration', () => {
  it('renders h/m for long, m/s for medium, s for short', () => {
    expect(formatDuration(5040)).toBe('1h 24m');
    expect(formatDuration(3600)).toBe('1h 0m');
    expect(formatDuration(1470)).toBe('24m 30s');
    expect(formatDuration(60)).toBe('1m');
    expect(formatDuration(45)).toBe('45s');
    expect(formatDuration(0)).toBe('0s');
  });

  it('renders a dash for null/negative/NaN', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(-5)).toBe('—');
    expect(formatDuration('x')).toBe('—');
  });
});
