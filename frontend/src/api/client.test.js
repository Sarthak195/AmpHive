/**
 * API client tests (api/client.js): apiRequest's request/response contract —
 * JWT header attach, 2xx JSON parsing, non-2xx error surfacing in the
 * {status, code, message} shape apiErrorCopy consumes, and network/abort
 * failures propagating as errors instead of hanging silently.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

import { apiRequest, api } from './client';

const jsonResponse = (body, { ok = true, status = ok ? 200 : 400 } = {}) => ({
  ok,
  status,
  json: () => Promise.resolve(body),
});

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn());
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('apiRequest — success', () => {
  it('parses and returns the JSON body on a 2xx response', async () => {
    fetch.mockResolvedValue(jsonResponse({ id: 1, name: 'Plug A' }));

    const data = await apiRequest('/api/plugs/1');

    expect(data).toEqual({ id: 1, name: 'Plug A' });
  });

  it('sends the method, JSON content-type, body, and an abort signal', async () => {
    fetch.mockResolvedValue(jsonResponse({}));

    await apiRequest('/api/plugs', { method: 'POST', body: JSON.stringify({ name: 'x' }) });

    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/api/plugs'),
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ name: 'x' }),
        headers: expect.objectContaining({ 'Content-Type': 'application/json' }),
        signal: expect.any(AbortSignal),
      })
    );
  });
});

describe('apiRequest — auth header attach', () => {
  it('attaches the Bearer token when one is stored', async () => {
    localStorage.setItem('amphive_token', 'jwt-abc');
    fetch.mockResolvedValue(jsonResponse({}));

    await apiRequest('/api/auth/me');

    const [, options] = fetch.mock.calls[0];
    expect(options.headers.Authorization).toBe('Bearer jwt-abc');
  });

  it('omits the Authorization header when no token is stored', async () => {
    fetch.mockResolvedValue(jsonResponse({}));

    await apiRequest('/api/public/ping');

    const [, options] = fetch.mock.calls[0];
    expect(options.headers.Authorization).toBeUndefined();
  });
});

describe('apiRequest — non-2xx error surfacing', () => {
  it('throws with the plain-string detail as the message and the status attached', async () => {
    fetch.mockResolvedValue(jsonResponse({ detail: 'Insufficient balance' }, { ok: false, status: 402 }));

    await expect(apiRequest('/api/sessions/start')).rejects.toMatchObject({
      message: 'Insufficient balance',
      status: 402,
    });
  });

  it('flattens FastAPI 422 validation-error arrays into "field: message" text', async () => {
    fetch.mockResolvedValue(
      jsonResponse(
        { detail: [{ loc: ['body', 'email'], msg: 'field required' }] },
        { ok: false, status: 422 }
      )
    );

    await expect(apiRequest('/api/auth/signup')).rejects.toMatchObject({
      message: 'email: field required',
      status: 422,
    });
  });

  it('surfaces structured {code, message} detail as err.code/err.message for apiErrorCopy', async () => {
    fetch.mockResolvedValue(
      jsonResponse(
        { detail: { code: 'circuit_full', message: 'This circuit is at capacity right now' } },
        { ok: false, status: 409 }
      )
    );

    await expect(apiRequest('/api/sessions/start')).rejects.toMatchObject({
      code: 'circuit_full',
      message: 'This circuit is at capacity right now',
      status: 409,
    });
  });

  it('falls back to a generic status message when the body has no detail', async () => {
    fetch.mockResolvedValue(jsonResponse(null, { ok: false, status: 500 }));

    await expect(apiRequest('/api/plugs')).rejects.toMatchObject({
      message: 'Request failed with status 500',
      status: 500,
    });
  });
});

describe('apiRequest — network failures', () => {
  it('propagates a raw network error (fetch rejection) unchanged', async () => {
    fetch.mockRejectedValue(new TypeError('Failed to fetch'));

    await expect(apiRequest('/api/plugs')).rejects.toThrow('Failed to fetch');
  });

  it('turns an aborted request into a friendly timeout error', async () => {
    const abortErr = new Error('The operation was aborted');
    abortErr.name = 'AbortError';
    fetch.mockRejectedValue(abortErr);

    await expect(apiRequest('/api/plugs')).rejects.toMatchObject({
      code: 'timeout',
      message: 'The request timed out. Check your connection and try again.',
    });
  });
});

describe('api convenience methods', () => {
  it('get/post/put/patch send the right method and JSON-stringified body', async () => {
    fetch.mockResolvedValue(jsonResponse({ ok: true }));

    await api.get('/api/plugs');
    await api.post('/api/plugs', { name: 'x' });
    await api.put('/api/plugs/1', { name: 'y' });
    await api.patch('/api/plugs/1', { name: 'z' });

    expect(fetch).toHaveBeenNthCalledWith(1, expect.stringContaining('/api/plugs'), expect.objectContaining({ method: 'GET' }));
    expect(fetch).toHaveBeenNthCalledWith(
      2,
      expect.stringContaining('/api/plugs'),
      expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'x' }) })
    );
    expect(fetch).toHaveBeenNthCalledWith(
      3,
      expect.stringContaining('/api/plugs/1'),
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ name: 'y' }) })
    );
    expect(fetch).toHaveBeenNthCalledWith(
      4,
      expect.stringContaining('/api/plugs/1'),
      expect.objectContaining({ method: 'PATCH', body: JSON.stringify({ name: 'z' }) })
    );
  });

  it('delete omits the body when none is given, and includes it when provided', async () => {
    fetch.mockResolvedValue(jsonResponse({ ok: true }));

    await api.delete('/api/sessions/queue/9');
    await api.delete('/api/groups/1/members', { user_id: 5 });

    expect(fetch.mock.calls[0][1]).not.toHaveProperty('body');
    expect(fetch.mock.calls[1][1]).toMatchObject({ body: JSON.stringify({ user_id: 5 }) });
  });
});
