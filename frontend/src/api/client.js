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
    const errorMessage = data?.detail || `Request failed with status ${response.status}`;
    throw new Error(errorMessage);
  }

  return data;
}

// --- Convenience methods ---

export const api = {
  get: (endpoint) => apiRequest(endpoint, { method: 'GET' }),
  post: (endpoint, body) => apiRequest(endpoint, { method: 'POST', body: JSON.stringify(body) }),
  put: (endpoint, body) => apiRequest(endpoint, { method: 'PUT', body: JSON.stringify(body) }),
  delete: (endpoint) => apiRequest(endpoint, { method: 'DELETE' }),
};

export default api;
