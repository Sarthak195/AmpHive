/**
 * plugSearch — client-side charger search/autocomplete.
 * =======================================================
 * No new endpoint, no geocoding: filters the already-fetched `plugs` array
 * (Dashboard's `/api/plugs/available`, MapPage's `/api/plugs/available` or
 * `/api/plugs/public`) against a free-text query. Shared by MapPage's list
 * search and Dashboard's charger filter so "what counts as a match" lives in
 * exactly one place.
 *
 * Matches (case/whitespace-insensitive substring):
 * - `plug.name`
 * - `String(plug.id)` — the same numeric ID printed on the charger's QR
 *   label and typed into the "Charge now" lookup.
 * - `plug.group_name`, when present (a driver searching "Sunrise" to find
 *   their society's chargers).
 */

/** lowercase + trim, so callers never repeat this normalization. */
function normalize(s) {
  return String(s ?? '').toLowerCase().trim();
}

/**
 * True when `query` (free text) matches `plug` on name, id, or group_name.
 * An empty/whitespace-only query matches everything (the "no filter" case).
 */
export function matchesQuery(plug, query) {
  const q = normalize(query);
  if (!q) return true;
  if (!plug) return false;

  if (normalize(plug.name).includes(q)) return true;
  if (normalize(plug.id).includes(q)) return true;
  if (plug.group_name && normalize(plug.group_name).includes(q)) return true;

  return false;
}

/* --- Bucketed spec/price filters (Dashboard + MapPage filter bars) --------
 * One shared definition so both surfaces render identical options and agree
 * on the boundaries. Power buckets read `rated_power_w` (the CPO-advertised
 * spec — null on plugs whose operator hasn't filled it in; those only match
 * "Any"). Price buckets read `price_per_kwh`, which every plug response
 * already carries (coins ≈ ₹ at the app's 1:1 display rate — the same
 * convention the Money primitive renders).
 */

export const POWER_BUCKETS = [
  { value: '', label: 'Any power' },
  { value: 'lte3300', label: '≤3.3 kW', test: (w) => w <= 3300 },
  { value: '3300to7400', label: '3.3–7.4 kW', test: (w) => w > 3300 && w <= 7400 },
  { value: 'gt7400', label: '7.4 kW+', test: (w) => w > 7400 },
];

export const PRICE_BUCKETS = [
  { value: '', label: 'Any price' },
  { value: 'lt10', label: 'Under ₹10/kWh', test: (p) => p < 10 },
  { value: '10to20', label: '₹10–20/kWh', test: (p) => p >= 10 && p <= 20 },
  { value: 'gt20', label: '₹20+/kWh', test: (p) => p > 20 },
];

function matchesBucket(buckets, value, raw) {
  if (!value) return true; // "Any" — no filter
  const bucket = buckets.find((b) => b.value === value);
  if (!bucket?.test) return true; // unknown value — fail open, don't hide data
  const n = Number(raw);
  if (raw == null || Number.isNaN(n)) return false; // unset spec only matches "Any"
  return bucket.test(n);
}

/** True when `plug.rated_power_w` falls in the selected POWER_BUCKETS value. */
export function matchesPowerBucket(plug, bucketValue) {
  return matchesBucket(POWER_BUCKETS, bucketValue, plug?.rated_power_w);
}

/** True when `plug.price_per_kwh` falls in the selected PRICE_BUCKETS value. */
export function matchesPriceBucket(plug, bucketValue) {
  return matchesBucket(PRICE_BUCKETS, bucketValue, plug?.price_per_kwh);
}

/**
 * The distinct non-null connector_type values in a fetched plug list,
 * sorted — the connector filter renders only when this has >1 entry (a
 * single-connector fleet has nothing to filter by).
 */
export function distinctConnectors(plugs) {
  const seen = new Set();
  for (const p of plugs || []) {
    if (p?.connector_type) seen.add(p.connector_type);
  }
  return Array.from(seen).sort((a, b) => a.localeCompare(b));
}
