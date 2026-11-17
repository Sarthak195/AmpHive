/**
 * InsightsCard tests: the CO2 figure follows the same rule as the receipt and
 * the CPO tile: shown only when net CO2 avoided is positive.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import InsightsCard from './InsightsCard';

const SERIES = [{ month: '2026-11', energy_kwh: 4, spend_coins: 20, sessions: 2 }];
const ASSUMPTIONS = {
  ev_km_per_kwh: 6, ice_km_per_l: 15, petrol_co2_kg_per_l: 2.31,
  petrol_price_per_l: 100, grid_emission_factor_kg_per_kwh: 0.71,
};
const impact = (co2) => ({
  assumptions: ASSUMPTIONS,
  lifetime: { co2_avoided_kg: co2, petrol_litres: 4.5, petrol_equivalent_inr: 450 },
});

describe('InsightsCard CO2 rule', () => {
  it('shows CO2 when net avoided is positive', () => {
    render(<InsightsCard series={SERIES} impact={impact(12.3)} />);
    expect(screen.getByText('≈ 12.3 kg')).toBeInTheDocument();
  });

  it.each([0, -2.5])('hides CO2 (and keeps petrol) when net avoided is %s', (co2) => {
    render(<InsightsCard series={SERIES} impact={impact(co2)} />);
    expect(screen.queryByText(/CO₂ avoided, all time/)).toBeNull();
    expect(screen.getByText(/Petrol not burned/)).toBeInTheDocument();
  });
});
