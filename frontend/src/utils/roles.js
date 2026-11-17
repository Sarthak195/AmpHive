/**
 * Role helpers (F10 read-only CPO viewer).
 *
 * The one place the SPA decides who may enter the operator console and who
 * may change things in it. The server is the real boundary (a cpo_viewer gets
 * 403 "read_only_demo" on every write); these only shape the UI.
 */

export const CPO_VIEWER = 'cpo_viewer';

export const READ_ONLY_DEMO_MESSAGE =
  'This is a read-only demo login, so changes are turned off.';

/** cpo, admin and the read-only cpo_viewer may open the CPO console. */
export function hasCpoConsole(user) {
  const role = user?.role;
  return role === 'cpo' || role === 'admin' || role === CPO_VIEWER;
}

/** True for the read-only demo viewer: hide or disable every write control. */
export function isReadOnlyViewer(user) {
  return user?.role === CPO_VIEWER;
}
