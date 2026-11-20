/**
 * GatewayChecklist tests: the sequence guard. A slower earlier response must
 * never overwrite a newer one when the gateway changes mid-request (Re-check
 * is disabled while a request is in flight), and Re-check shows the new answer.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import GatewayChecklist from './GatewayChecklist';
import api from '../api/client';

vi.mock('../api/client', () => ({
  default: { get: vi.fn() },
}));

const report = (detail) => ({
  overall: 'pass',
  checks: [{ key: 'online', label: 'Gateway online', status: 'pass', detail, fix_hint: null }],
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('GatewayChecklist request ordering', () => {
  it('a slow response for the previous gateway does not overwrite the new one', async () => {
    const first = deferred();
    const second = deferred();
    api.get.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    const { rerender } = render(<GatewayChecklist gatewayId="gw-a" />);
    rerender(<GatewayChecklist gatewayId="gw-b" />);

    await act(async () => {
      second.resolve(report('gateway B is fine'));
    });
    expect(await screen.findByText('gateway B is fine')).toBeInTheDocument();

    await act(async () => {
      first.resolve(report('gateway A is stale'));
    });
    expect(screen.getByText('gateway B is fine')).toBeInTheDocument();
    expect(screen.queryByText('gateway A is stale')).not.toBeInTheDocument();
    expect(api.get).toHaveBeenNthCalledWith(1, '/api/cpo/gateways/gw-a/selftest');
    expect(api.get).toHaveBeenNthCalledWith(2, '/api/cpo/gateways/gw-b/selftest');
  });

  it('Re-check replaces the shown report with the newer answer', async () => {
    const first = deferred();
    const second = deferred();
    api.get.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    render(<GatewayChecklist gatewayId="gw-a" />);
    // Re-check is disabled while a request is in flight, so it is pressed
    // once the first answer has landed.
    await act(async () => {
      first.resolve(report('first answer'));
    });
    expect(await screen.findByText('first answer')).toBeInTheDocument();

    await userEvent.click(screen.getByRole('button', { name: /re-check/i }));
    await act(async () => {
      second.resolve(report('second answer'));
    });
    await waitFor(() => expect(screen.getByText('second answer')).toBeInTheDocument());
    expect(screen.queryByText('first answer')).not.toBeInTheDocument();
  });

  it('a stale failure does not replace a newer success with an error', async () => {
    const first = deferred();
    const second = deferred();
    api.get.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);

    const { rerender } = render(<GatewayChecklist gatewayId="gw-a" />);
    rerender(<GatewayChecklist gatewayId="gw-b" />);
    await act(async () => {
      second.resolve(report('gateway B is fine'));
    });
    await act(async () => {
      first.reject(new Error('timeout'));
    });
    expect(screen.getByText('gateway B is fine')).toBeInTheDocument();
    expect(screen.queryByText(/timeout/i)).not.toBeInTheDocument();
  });
});
