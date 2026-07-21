/**
 * Marketing homepage tests — behavioral coverage of the C1 anatomy:
 * hero + CTAs, live network proof (real counts from /api/plugs/public,
 * hidden entirely on fetch error — never faked, never an error block),
 * the bay-label plug-ID funnel into /login?next=/?plug=<id>, the anonymous
 * /?plug= deep-link banner, the volt-themed for-hosts band, and the
 * real-links-only footer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';

import Marketing from './Marketing';
import api from '../api/client';
import { cpoOrigin } from '../utils/appHost';

vi.mock('../api/client', () => {
  const api = { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() };
  return { api, default: api, apiRequest: vi.fn() };
});

/** Probe for asserting where the plug-ID funnel navigates. */
const LoginProbe = () => {
  const location = useLocation();
  return <div>login page {location.search}</div>;
};

const renderPage = (entry = '/') =>
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route path="/" element={<Marketing />} />
        <Route path="/login" element={<LoginProbe />} />
      </Routes>
    </MemoryRouter>
  );

/** Wait for the public-plugs fetch to settle so no state updates leak. */
const settleNetworkFetch = () =>
  waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/plugs/public'));

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue([]);
});

describe('Marketing hero', () => {
  it('renders the headline and both CTAs with real destinations', async () => {
    renderPage();

    expect(
      screen.getByRole('heading', { level: 1, name: 'The charger was already there.' })
    ).toBeInTheDocument();

    // Hero CTA + footer link share the label — both must point at /map.
    for (const link of screen.getAllByRole('link', { name: 'Find a charger' })) {
      expect(link).toHaveAttribute('href', '/map');
    }
    expect(screen.getByRole('link', { name: 'Host your chargers' })).toHaveAttribute(
      'href', `${cpoOrigin()}/cpo`
    );
    await settleNetworkFetch();
  });

  it('funnels a typed Plug ID to login with the deep link preserved', async () => {
    renderPage();
    await settleNetworkFetch();

    const input = screen.getByLabelText(
      'Already at a charger? Type the Plug ID printed on the label'
    );
    await userEvent.type(input, '42');
    await userEvent.click(screen.getByRole('button', { name: 'Start' }));

    expect(await screen.findByText(/login page/)).toHaveTextContent(
      `login page ?next=${encodeURIComponent('/?plug=42')}`
    );
  });

  it('keeps Start disabled until a numeric Plug ID is entered', async () => {
    renderPage();
    await settleNetworkFetch();

    const start = screen.getByRole('button', { name: 'Start' });
    expect(start).toBeDisabled();

    // Non-digits are stripped — still nothing to submit.
    const input = screen.getByLabelText(
      'Already at a charger? Type the Plug ID printed on the label'
    );
    await userEvent.type(input, 'abc');
    expect(input).toHaveValue('');
    expect(start).toBeDisabled();

    await userEvent.type(input, '7');
    expect(start).toBeEnabled();
  });
});

describe('live network proof', () => {
  it('shows real counts from /api/plugs/public', async () => {
    api.get.mockResolvedValue([
      { id: 1, status: 'available', gateway_online: true },
      { id: 2, status: 'occupied', gateway_online: true },
      { id: 3, status: 'available', gateway_online: false },
    ]);
    renderPage();

    const proof = await screen.findByTestId('network-proof');
    expect(proof).toHaveTextContent('3 chargers on the network · 1 available right now');
    expect(api.get).toHaveBeenCalledWith('/api/plugs/public');
  });

  it('hides the line entirely when the fetch fails — no fake counts, no error block', async () => {
    api.get.mockRejectedValue(new Error('network down'));
    renderPage();
    await settleNetworkFetch();

    await waitFor(() =>
      expect(screen.queryByTestId('network-proof')).not.toBeInTheDocument()
    );
    expect(screen.queryByText(/couldn't load/i)).not.toBeInTheDocument();
    // The rest of the page is unaffected.
    expect(screen.getByRole('heading', { level: 1 })).toBeInTheDocument();
  });

  it('hides the line for an empty network (zero is not proof)', async () => {
    api.get.mockResolvedValue([]);
    renderPage();
    await settleNetworkFetch();

    await waitFor(() =>
      expect(screen.queryByTestId('network-proof')).not.toBeInTheDocument()
    );
  });
});

describe('anonymous /?plug= deep link', () => {
  it('shows the sign-in funnel banner above the hero', async () => {
    renderPage('/?plug=7');

    expect(screen.getByText('Charger #7 — sign in to start')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Sign in to start' })).toHaveAttribute(
      'href', `/login?next=${encodeURIComponent('/?plug=7')}`
    );
    await settleNetworkFetch();
  });

  it('renders no banner without the param', async () => {
    renderPage();
    expect(screen.queryByText(/sign in to start/i)).not.toBeInTheDocument();
    await settleNetworkFetch();
  });
});

describe('page sections', () => {
  it('renders how-it-works, for-drivers, the volt hosts band and the safety strip', async () => {
    renderPage();

    expect(screen.getByRole('heading', { name: 'How it works' })).toBeInTheDocument();
    expect(
      screen.getByText(/reservations hold the slot for you — you still start the charge yourself/i)
    ).toBeInTheDocument();

    expect(screen.getByRole('heading', { name: 'For drivers' })).toBeInTheDocument();
    expect(screen.getByText('Live cost meter')).toBeInTheDocument();

    const hostsHeading = screen.getByRole('heading', {
      name: 'Own a parking spot with a plug point? Earn from it.',
    });
    // The hosts band literally previews the console atmosphere.
    expect(hostsHeading.closest('[data-theme="volt"]')).not.toBeNull();
    expect(screen.getByRole('link', { name: 'Open the host console' })).toHaveAttribute(
      'href', `${cpoOrigin()}/cpo`
    );

    expect(screen.getByText(/hosts never open up their network/i)).toBeInTheDocument();
    await settleNetworkFetch();
  });

  it('footer contains real links only', async () => {
    renderPage();
    const footer = screen.getByRole('contentinfo');

    const expectHref = (name, href) =>
      expect(within(footer).getByRole('link', { name })).toHaveAttribute('href', href);

    expectHref('Find a charger', '/map');
    expectHref('Wallet', '/wallet');
    expectHref('Activity', '/activity');
    expectHref('Host console', `${cpoOrigin()}/cpo/dashboard`);
    expectHref('Become a host', `${cpoOrigin()}/cpo`);
    expectHref('Sign in', '/login');
    expectHref('Create account', '/signup');

    // No fabricated marketing links.
    expect(within(footer).queryByRole('link', { name: /about|careers|blog/i })).toBeNull();
    await settleNetworkFetch();
  });
});
