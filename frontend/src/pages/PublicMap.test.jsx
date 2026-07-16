/**
 * PublicMap tests: the pre-signup discovery map loads public chargers from the
 * unauthenticated endpoint, shows a count + availability summary, and routes
 * every action (page CTA + in-map "Sign up to charge") to sign-in.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';

import PublicMap from './PublicMap';
import api from '../api/client';

const navigateMock = vi.fn();
vi.mock('react-router-dom', async (importOriginal) => ({
  ...(await importOriginal()),
  useNavigate: () => navigateMock,
}));

vi.mock('../api/client', () => ({ default: { get: vi.fn() } }));

// Mock the leaflet map — assert the props PublicMap hands it without rendering
// react-leaflet in jsdom.
vi.mock('../components/MapComponent', () => ({
  default: ({ plugs, selectLabel, onPlugSelect }) => (
    <div data-testid="map">
      <span>markers:{plugs.length}</span>
      <button onClick={() => onPlugSelect(plugs[0]?.id)}>{selectLabel}</button>
    </div>
  ),
}));

const PLUGS = [
  { id: 1, name: 'Public A', status: 'available', latitude: 12.9, longitude: 77.6, price_per_kwh: 5, gateway_online: true },
  { id: 2, name: 'Public B', status: 'occupied', latitude: 13.0, longitude: 77.5, price_per_kwh: 6, gateway_online: true },
];

beforeEach(() => vi.clearAllMocks());

const renderPage = () => render(<MemoryRouter><PublicMap /></MemoryRouter>);

describe('PublicMap', () => {
  it('loads public chargers from the unauthenticated endpoint with a count summary', async () => {
    api.get.mockResolvedValue(PLUGS);
    renderPage();

    expect(await screen.findByText(/2 chargers · 1 available/)).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/plugs/public');
    expect(screen.getByTestId('map')).toHaveTextContent('markers:2');
  });

  it('routes the "Sign in to charge" CTA to /login', async () => {
    api.get.mockResolvedValue(PLUGS);
    renderPage();
    await screen.findByTestId('map');

    await userEvent.click(screen.getByRole('button', { name: 'Sign in to charge' }));
    expect(navigateMock).toHaveBeenCalledWith('/login');
  });

  it('passes a sign-up CTA into the map that also routes to /login', async () => {
    api.get.mockResolvedValue(PLUGS);
    renderPage();
    await screen.findByTestId('map');

    // The label PublicMap passes as selectLabel — proves the in-map action is signup, not "Select".
    await userEvent.click(screen.getByRole('button', { name: 'Sign up to charge' }));
    expect(navigateMock).toHaveBeenCalledWith('/login');
  });

  it('shows an empty state when there are no public chargers', async () => {
    api.get.mockResolvedValue([]);
    renderPage();
    expect(await screen.findByText(/No public chargers/)).toBeInTheDocument();
  });

  it('surfaces a fetch error inline', async () => {
    api.get.mockRejectedValue(new Error('network down'));
    renderPage();
    expect(await screen.findByText('network down')).toBeInTheDocument();
  });
});
