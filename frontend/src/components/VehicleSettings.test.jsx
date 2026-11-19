/**
 * VehicleSettings tests: loads the saved figures and the standard values as
 * placeholders, saves edited figures (empty = null) with the monthly-email
 * opt-in, reports a rejected save, and a read-only cpo_viewer sees nothing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import VehicleSettings from './VehicleSettings';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('./ui', () => ({ useToast: () => toast }));

let mockUser;
vi.mock('../contexts/AuthContext', () => ({ useAuth: () => ({ user: mockUser }) }));

const SAVED = {
  km_per_kwh: 8,
  petrol_km_per_l: null,
  petrol_price_per_l: null,
  monthly_insights_email: false,
  defaults: { km_per_kwh: 6, petrol_km_per_l: 15, petrol_price_per_l: 100 },
};

beforeEach(() => {
  vi.clearAllMocks();
  mockUser = { role: 'driver' };
  api.get.mockResolvedValue(SAVED);
  api.put.mockImplementation(async (_url, body) => ({ ...SAVED, ...body }));
});

describe('VehicleSettings', () => {
  it('shows the saved figure and the standard values as placeholders', async () => {
    render(<VehicleSettings />);
    expect(await screen.findByLabelText('Your vehicle, km per kWh')).toHaveValue(8);
    const replaced = screen.getByLabelText(/Petrol vehicle replaced/);
    expect(replaced).toHaveValue(null);
    expect(replaced).toHaveAttribute('placeholder', 'Standard: 15');
    expect(screen.getByLabelText('Email me a monthly charging summary')).not.toBeChecked();
    expect(api.get).toHaveBeenCalledWith('/api/me/vehicle');
  });

  it('saves edited figures, empty fields as null, and the email opt-in', async () => {
    const user = userEvent.setup();
    render(<VehicleSettings />);
    await user.type(await screen.findByLabelText(/Petrol price/), '105.5');
    await user.click(screen.getByLabelText('Email me a monthly charging summary'));
    await user.click(screen.getByRole('button', { name: 'Save vehicle settings' }));
    await waitFor(() => expect(api.put).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith('/api/me/vehicle', {
      km_per_kwh: 8,
      petrol_km_per_l: null,
      petrol_price_per_l: 105.5,
      monthly_insights_email: true,
    });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('reports a rejected save', async () => {
    api.put.mockRejectedValue(new Error('bad'));
    const user = userEvent.setup();
    render(<VehicleSettings />);
    await user.click(await screen.findByRole('button', { name: 'Save vehicle settings' }));
    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(toast.ok).not.toHaveBeenCalled();
  });

  it('is not shown to a read-only viewer', async () => {
    mockUser = { role: 'cpo_viewer' };
    const { container } = render(<VehicleSettings />);
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
    expect(api.get).not.toHaveBeenCalled();
  });
});
