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

// [Scheduled start] Times in the TENANT's zone (an IANA name from the API),
// not the viewer's: the tariff's off-peak hours are tenant wall-clock.

// "22:00" rendered in zone `tz`.
export const fmtTimeInZone = (iso, tz) =>
  new Date(iso).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', timeZone: tz });

// Offset (ms) of `tz` from UTC at instant `ms`.
const tzOffsetMs = (ms, tz) => {
  const p = Object.fromEntries(
    new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hourCycle: 'h23', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    })
      .formatToParts(new Date(ms))
      .map((x) => [x.type, x.value])
  );
  return Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second) - ms;
};

// Wall-clock y-m-d h:m in `tz` -> instant (ms). Date.UTC normalizes d + 1 past
// a month end; the second pass settles a DST edge (a time in the spring gap or
// the autumn overlap resolves to one instant, which the hint line shows).
const zonedToMs = (y, mo, d, h, m, tz) => {
  const guess = Date.UTC(y, mo - 1, d, h, m);
  return guess - tzOffsetMs(guess - tzOffsetMs(guess, tz), tz);
};

// Next occurrence of wall-clock "HH:MM" in `tz` strictly after `nowMs`
// -> { iso, tomorrow }. "Tomorrow" is the next LOCAL date, not now + 24 h,
// so a 23 h or 25 h DST day still lands on the right calendar day.
export const nextOccurrenceInZone = (hhmm, tz, nowMs = Date.now()) => {
  const [h, m] = hhmm.split(':').map(Number);
  const [y, mo, d] = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
  })
    .format(new Date(nowMs))
    .split('-')
    .map(Number);
  let t = zonedToMs(y, mo, d, h, m, tz);
  const tomorrow = t <= nowMs;
  if (tomorrow) t = zonedToMs(y, mo, d + 1, h, m, tz);
  return { iso: new Date(t).toISOString(), tomorrow };
};
