/**
 * Host-partition routing tests: CpoLanding role routing on the CPO host
 * ("/" → login / dashboard / not-an-operator message) and ExternalRedirect's
 * cross-origin bounce link (jsdom can't actually navigate, so the visible
 * fallback anchor is what gets asserted).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';

import { CpoLanding, ExternalRedirect } from './HostRouting';
import { useAuth } from '../contexts/AuthContext';

vi.mock('../contexts/AuthContext', () => ({
  useAuth: vi.fn(),
}));

const renderLanding = () =>
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={<CpoLanding />} />
        <Route path="/login" element={<div>login page</div>} />
        <Route path="/cpo/dashboard" element={<div>cpo dashboard</div>} />
        <Route path="/cpo" element={<div>cpo setup page</div>} />
      </Routes>
    </MemoryRouter>
  );

beforeEach(() => {
  vi.clearAllMocks();
});

describe('CpoLanding', () => {
  it('sends anonymous visitors to /login', () => {
    useAuth.mockReturnValue({ user: null });
    renderLanding();
    expect(screen.getByText('login page')).toBeInTheDocument();
  });

  it.each(['cpo', 'admin'])('sends the %s role to the dashboard', (role) => {
    useAuth.mockReturnValue({ user: { email: 'op@amphive.test', role } });
    renderLanding();
    expect(screen.getByText('cpo dashboard')).toBeInTheDocument();
  });

  it('shows the not-an-operator message to driver-role logins, with a driver-origin link and a become-a-host link', () => {
    useAuth.mockReturnValue({ user: { email: 'driver@amphive.test', role: 'driver' } });
    renderLanding();
    expect(screen.getByText('This account is not an operator account')).toBeInTheDocument();
    const driverLink = screen.getByRole('link', { name: /go to the driver app/i });
    // jsdom hostname has no cpo. prefix, so the derived driver origin is the
    // current origin — the important part is it's an absolute external href.
    expect(driverLink).toHaveAttribute('href', window.location.origin);
    expect(screen.getByRole('link', { name: /apply to become a host/i })).toHaveAttribute('href', '/cpo');
  });
});

describe('ExternalRedirect', () => {
  it('renders a moved-notice link to the same path on the counterpart origin', () => {
    render(
      <MemoryRouter initialEntries={['/cpo/plugs?x=1']}>
        <Routes>
          <Route path="/cpo/*" element={<ExternalRedirect origin="https://cpo.amphive.duckdns.org" />} />
        </Routes>
      </MemoryRouter>
    );
    expect(screen.getByRole('link')).toHaveAttribute(
      'href',
      'https://cpo.amphive.duckdns.org/cpo/plugs?x=1'
    );
  });
});
