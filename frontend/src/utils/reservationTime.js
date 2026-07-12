/**
 * Reservation time formatting helpers — shared by Home's "Your reservations"
 * strip / plug-card "Reserved until" badge and ReserveModal's schedule list.
 * The API sends ISO-8601 UTC timestamps; everything here renders in the
 * viewer's LOCAL time via toLocale*String (the backend deliberately doesn't
 * know the driver's timezone).
 */

// "14:05" in the viewer's local time.
export const fmtTime = (iso) =>
  new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

// "12 Jul, 14:05 – 15:05" (the end repeats its date only when it differs,
// e.g. a window crossing midnight: "12 Jul, 23:30 – 13 Jul, 01:30").
export const fmtWindow = (startIso, endIso) => {
  const dateOpts = { day: 'numeric', month: 'short' };
  const startDate = new Date(startIso).toLocaleDateString([], dateOpts);
  const endDate = new Date(endIso).toLocaleDateString([], dateOpts);
  const endPart =
    startDate === endDate ? fmtTime(endIso) : `${endDate}, ${fmtTime(endIso)}`;
  return `${startDate}, ${fmtTime(startIso)} – ${endPart}`;
};
