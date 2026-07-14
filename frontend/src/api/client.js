/**
 * AmpHive API Client
 * ==================
 * Centralized fetch wrapper for all backend API calls.
 * Automatically attaches JWT auth tokens and handles 401 redirects.
 *
 * Design decisions:
 * - Using native fetch (no Axios) to keep the bundle small.
 * - API base URL comes from VITE_API_URL env var or defaults to
 *   the current origin (for same-domain Docker deployments).
 * - JWT token is stored in localStorage under 'amphive_token'.
 * - On 401 response, the token is cleared and user is redirected
 *   to the login page to re-authenticate.
 */

// In development: Vite provides import.meta.env.VITE_API_URL
// In production (Docker): frontend is reverse-proxied to the same origin
const API_BASE = import.meta.env.VITE_API_URL || '';

/**
 * Make an authenticated API request.
 *
 * @param {string} endpoint - API path (e.g. '/api/auth/login')
 * @param {object} options - Fetch options (method, body, headers, etc.)
 * @returns {Promise<any>} - Parsed JSON response
 * @throws {Error} - Throws on non-2xx responses with the error detail
 */
export async function apiRequest(endpoint, options = {}) {
  const token = localStorage.getItem('amphive_token');

  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  };

  // Attach JWT token if available
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    headers,
  });

  // Handle 401 Unauthorized — token expired or invalid
  if (response.status === 401) {
    localStorage.removeItem('amphive_token');
    localStorage.removeItem('amphive_user');
    // Redirect to login page if not already there
    if (window.location.pathname !== '/login') {
      window.location.href = '/login';
    }
    throw new Error('Authentication expired. Please sign in again.');
  }

  // Parse response
  const data = await response.json().catch(() => null);

  if (!response.ok) {
    let errorMessage = data?.detail;
    let errorCode;
    // FastAPI validation errors (422) send `detail` as a list of
    // {loc, msg, ...} objects — flatten to "field: message" lines so the UI
    // shows something readable instead of "[object Object]".
    if (Array.isArray(errorMessage)) {
      errorMessage = errorMessage
        .map((e) => {
          const field = Array.isArray(e?.loc) ? e.loc[e.loc.length - 1] : null;
          return field ? `${field}: ${e?.msg}` : e?.msg;
        })
        .filter(Boolean)
        .join('; ');
    } else if (errorMessage && typeof errorMessage === 'object') {
      // Structured error detail {code, message, ...} — surface the code so
      // callers can branch (e.g. "circuit_full" → offer "Request capacity").
      errorCode = errorMessage.code;
      errorMessage = errorMessage.message || JSON.stringify(errorMessage);
    }
    const err = new Error(errorMessage || `Request failed with status ${response.status}`);
    err.status = response.status;
    if (errorCode) err.code = errorCode;
    if (data?.detail && typeof data.detail === 'object' && !Array.isArray(data.detail)) {
      err.detail = data.detail; // full structured payload (e.g. plug_id)
    }
    throw err;
  }

  return data;
}

// --- Convenience methods ---

export const api = {
  get: (endpoint) => apiRequest(endpoint, { method: 'GET' }),
  post: (endpoint, body) => apiRequest(endpoint, { method: 'POST', body: JSON.stringify(body) }),
  put: (endpoint, body) => apiRequest(endpoint, { method: 'PUT', body: JSON.stringify(body) }),
  patch: (endpoint, body) => apiRequest(endpoint, { method: 'PATCH', body: JSON.stringify(body) }),
  delete: (endpoint, body) =>
    apiRequest(endpoint, {
      method: 'DELETE',
      ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
    }),

  // --- Queued charges ---
  // Queue a charge on an unpowered plug (gateway online, no line power). The
  // backend auto-starts it, after a CPO debounce, once power returns. Body:
  // { plug_id, max_kwh?, max_duration_seconds? }.
  queueCharge: (body) =>
    apiRequest('/api/sessions/queue', { method: 'POST', body: JSON.stringify(body) }),
  // The signed-in driver's WAITING queued charges (with expiry).
  listQueuedCharges: () => apiRequest('/api/sessions/queued', { method: 'GET' }),
  // Cancel a WAITING queued charge (owner-only).
  cancelQueuedCharge: (id) =>
    apiRequest(`/api/sessions/queue/${id}`, { method: 'DELETE' }),
};

export default api;
