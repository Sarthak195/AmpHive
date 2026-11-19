/**
 * safePath — guards `?next=`/`state.from`-style redirect targets against
 * open-redirect abuse. Only a same-app relative path is ever safe to hand to
 * navigate()/<Link>: a single leading '/' and nothing that a browser or
 * router could reinterpret as pointing at another origin — no
 * protocol-relative `//evil.com`, no backslash variant `/\evil.com` (some
 * URL parsers normalize `\` to `/`), and no absolute URL with any scheme
 * (`https:`, `javascript:`, etc.).
 */
export function isSafeInternalPath(p) {
  if (typeof p !== 'string' || p.length === 0) return false;
  if (p[0] !== '/') return false;
  if (p[1] === '/' || p[1] === '\\') return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(p)) return false;
  return true;
}

// The Google sign-in round trip is a full-page navigation through the backend,
// so router state (Login's state.from, e.g. /groups?invite=...) does not
// survive it. Login stashes the target here before the redirect and
// GoogleCallback takes it once afterwards. sessionStorage can be unavailable
// (private mode, blocked storage): every access is guarded and a miss means Home.
const POST_LOGIN_KEY = 'amphive.postLoginPath';

export function stashPostLoginPath(p) {
  try {
    if (isSafeInternalPath(p) && p !== '/') sessionStorage.setItem(POST_LOGIN_KEY, p);
    else sessionStorage.removeItem(POST_LOGIN_KEY);
  } catch {
    /* storage unavailable: the callback falls back to Home */
  }
}

export function takePostLoginPath() {
  let p = null;
  try {
    p = sessionStorage.getItem(POST_LOGIN_KEY);
    sessionStorage.removeItem(POST_LOGIN_KEY);
  } catch {
    return '/';
  }
  return isSafeInternalPath(p) ? p : '/';
}
