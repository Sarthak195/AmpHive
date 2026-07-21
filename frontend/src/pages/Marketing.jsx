/**
 * Marketing — the public homepage (anon "/", day theme).
 * ======================================================
 * The product's first impression. Anatomy (redesign-v3-pages.md §C1):
 * hero with the bay-label signature card + plug-ID funnel, live network
 * proof from GET /api/plugs/public (hidden entirely on fetch error — never
 * faked), numbered how-it-works, for-drivers cards, a volt-themed for-hosts
 * band (a literal preview of the console atmosphere), safety strip, and a
 * footer of real links only. Anonymous `/?plug=<id>` visitors get a
 * sign-in funnel banner above the hero.
 */

import { useCallback, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import {
  BellRing,
  Check,
  Gauge,
  Lock,
  PlugZap,
  PowerOff,
  ReceiptText,
  ShieldCheck,
  Zap,
} from 'lucide-react';
import api from '../api/client';
import usePoll from '../hooks/usePoll';
import { StatusDot } from '../components/ui';
import { getPlugAvailability } from '../utils/plugAvailability';
import { formatINR } from '../utils/money';
import { cpoOrigin } from '../utils/appHost';
import './Marketing.css';

const NETWORK_POLL_MS = 60_000;

/** Login deep link that returns the visitor to /?plug=<id> after sign-in. */
const loginNextTo = (plugId) => `/login?next=${encodeURIComponent(`/?plug=${plugId}`)}`;

/* ---- hero: bay-label sticker + plug-ID funnel ---------------------------- */

const BayLabelCard = () => {
  const navigate = useNavigate();
  const [plugId, setPlugId] = useState('');

  const submit = (e) => {
    e.preventDefault();
    if (!plugId) return;
    navigate(loginNextTo(plugId));
  };

  return (
    <>
      {/* The printed sticker drivers actually see on a bay — decorative
          twin of the form below (the big number mirrors what you type). */}
      <div className="mkt-sticker" aria-hidden="true">
        <div className="mkt-sticker-band">
          <Zap size={15} />
          AmpHive
        </div>
        <div className="mkt-sticker-inner">
          <span className="mkt-sticker-label">Plug ID</span>
          <span className="mkt-sticker-num">{plugId || '12'}</span>
          <span className="mkt-sticker-hint">Scan or type this ID to charge</span>
        </div>
      </div>

      <form className="mkt-plug-form" onSubmit={submit}>
        <label className="field-label" htmlFor="mkt-plug-id">
          Already at a charger? Type the Plug ID printed on the label
        </label>
        <div className="mkt-plug-form-row">
          <input
            id="mkt-plug-id"
            className="input"
            inputMode="numeric"
            pattern="[0-9]*"
            autoComplete="off"
            placeholder="e.g. 12"
            value={plugId}
            onChange={(e) => setPlugId(e.target.value.replace(/\D/g, '').slice(0, 8))}
          />
          <button type="submit" className="btn btn-primary" disabled={!plugId}>
            Start
          </button>
        </div>
      </form>
    </>
  );
};

/* ---- live network proof --------------------------------------------------
 * Real counts from /api/plugs/public or nothing at all: on fetch error (or
 * an empty network) the line disappears — it is never faked. */

const NetworkProof = () => {
  const [{ loading, plugs }, setState] = useState({ loading: true, plugs: null });

  const fetchNetwork = useCallback(async () => {
    try {
      const data = await api.get('/api/plugs/public');
      setState({ loading: false, plugs: Array.isArray(data) ? data : null });
    } catch {
      setState({ loading: false, plugs: null });
    }
  }, []);

  usePoll(fetchNetwork, NETWORK_POLL_MS);

  if (loading) {
    return <div className="skeleton skeleton-text mkt-proof-skeleton" aria-hidden="true" />;
  }
  if (!plugs || plugs.length === 0) return null;

  const available = plugs.filter((p) => getPlugAvailability(p) === 'available').length;

  return (
    <p className="mkt-proof" aria-live="polite" data-testid="network-proof">
      <StatusDot state="available" live />
      <span>
        <span className="num">{plugs.length}</span> charger{plugs.length === 1 ? '' : 's'} on the network ·{' '}
        <span className="num">{available}</span> available right now
      </span>
    </p>
  );
};

const Hero = () => (
  <section className="mkt-section mkt-hero-section" aria-labelledby="mkt-hero-h">
    <div className="container mkt-hero">
      <div className="mkt-hero-copy">
        <p className="mkt-eyebrow anim-rise-stagger">Shared EV charging</p>
        <h1 id="mkt-hero-h" className="mkt-hero-h anim-rise-stagger">
          The charger was already there.
        </h1>
        <p className="mkt-lede anim-rise-stagger">
          AmpHive turns the ordinary plug points in your society, office, or shop into a
          paid EV-charging network — a smart plug and a matchbox-sized hub instead of
          lakhs of new hardware.
        </p>
        <div className="mkt-ctas anim-rise-stagger">
          <Link to="/map" className="btn btn-primary btn-lg">Find a charger</Link>
          <a href={`${cpoOrigin()}/cpo`} className="btn btn-quiet btn-lg">Host your chargers</a>
        </div>
      </div>

      <div className="mkt-hero-visual anim-rise-stagger">
        <BayLabelCard />
        <NetworkProof />
      </div>
    </div>
  </section>
);

/* ---- anonymous /?plug= deep link (printed QR labels) ---------------------- */

const DeepLinkBanner = ({ plugId }) => (
  <div className="container mkt-deeplink-wrap">
    <div className="card mkt-deeplink">
      <PlugZap size={22} aria-hidden="true" />
      <div className="mkt-deeplink-copy">
        <strong>{`Charger #${plugId} — sign in to start`}</strong>
        <span className="text-2 text-sm">
          Sign in and we&apos;ll bring you straight back to this charger.
        </span>
      </div>
      <Link to={loginNextTo(plugId)} className="btn btn-primary">Sign in to start</Link>
    </div>
  </div>
);

/* ---- how it works ---------------------------------------------------------- */

const STEPS = [
  {
    title: 'Find or scan',
    body: 'Spot a charger on the map, or scan the bay label right in front of you.',
  },
  {
    title: 'Add charging credit',
    body: 'Add credit with UPI or cards — it pays only for charging on AmpHive.',
  },
  {
    title: 'Charge with a live meter',
    body: 'Cost and energy tick in real time — charging stops automatically at your limit or balance.',
  },
];

const HowItWorks = () => (
  <section className="mkt-section" aria-labelledby="mkt-how-h">
    <div className="container">
      <h2 id="mkt-how-h" className="mkt-h">How it works</h2>
      <ol className="mkt-steps">
        {STEPS.map((step, i) => (
          <li key={step.title}>
            <span className="mkt-step-num num" aria-hidden="true">{i + 1}</span>
            <h3>{step.title}</h3>
            <p className="text-2">{step.body}</p>
          </li>
        ))}
      </ol>
      <p className="mkt-footnote">
        Reservations hold the slot for you — you still start the charge yourself.
      </p>
    </div>
  </section>
);

/* ---- for drivers ----------------------------------------------------------- */

const DRIVER_CARDS = [
  {
    icon: Gauge,
    title: 'Live cost meter',
    body: '₹ ticks as you charge, kWh by kWh — you always know what you owe before you unplug.',
  },
  {
    icon: BellRing,
    title: 'Reserve, or get notified',
    body: 'Hold a slot before you arrive, or ask to be notified the moment a busy charger frees up.',
  },
  {
    icon: ReceiptText,
    title: 'Charging credit with receipts',
    body: 'Prepaid charging credit with a receipt for every session — GST invoices included when you need them.',
  },
];

const ForDrivers = () => (
  <section className="mkt-section" aria-labelledby="mkt-drivers-h">
    <div className="container">
      <h2 id="mkt-drivers-h" className="mkt-h">For drivers</h2>
      <ul className="mkt-driver-cards">
        {DRIVER_CARDS.map(({ icon: Icon, title, body }) => (
          <li key={title} className="card">
            <span className="mkt-card-icon">
              <Icon size={20} aria-hidden="true" />
            </span>
            <h3>{title}</h3>
            <p className="text-2 text-sm">{body}</p>
          </li>
        ))}
      </ul>
    </div>
  </section>
);

/* ---- for hosts: a literal preview of the volt console atmosphere ---------- */

const HOST_POINTS = [
  'Set your own ₹/kWh, with time-of-day pricing',
  'Private access codes or a public listing',
  'Live earnings, faults, and GST paperwork in one console',
];

const ForHosts = () => (
  <section className="mkt-volt" data-theme="volt" aria-labelledby="mkt-hosts-h">
    <div className="container mkt-section mkt-volt-grid">
      <div className="mkt-volt-copy">
        <p className="mkt-eyebrow">For hosts</p>
        <h2 id="mkt-hosts-h" className="mkt-h">
          Own a parking spot with a plug point? Earn from it.
        </h2>
        <ul className="mkt-volt-list">
          {HOST_POINTS.map((point) => (
            <li key={point}>
              <Check size={18} aria-hidden="true" />
              {point}
            </li>
          ))}
        </ul>
        <a href={`${cpoOrigin()}/cpo`} className="btn btn-primary btn-lg">
          Open the host console
        </a>
      </div>

      {/* Illustrative console mock — HTML only, no images, purely decorative. */}
      <div className="card mkt-console-mock" aria-hidden="true">
        <div className="mkt-mock-title">
          <Zap size={13} />
          Host console
        </div>
        <div className="mkt-mock-kpis">
          <div className="kpi">
            <span className="kpi-label">Today</span>
            <span className="kpi-value">{formatINR(412.5)}</span>
            <span className="kpi-sub">6 sessions</span>
          </div>
          <div className="kpi">
            <span className="kpi-label">Energy</span>
            <span className="kpi-value">18.2 kWh</span>
            <span className="kpi-sub">2 charging now</span>
          </div>
        </div>
        <div className="mkt-mock-meter">
          <div className="row-between text-sm">
            <span>Circuit load</span>
            <span className="num">9.6 / 15 A</span>
          </div>
          <div className="meter">
            <div className="meter-fill" />
          </div>
        </div>
      </div>
    </div>
  </section>
);

/* ---- safety strip ----------------------------------------------------------- */

const SAFETY_POINTS = [
  { icon: ShieldCheck, text: 'Outbound-only connectivity — hosts never open up their network' },
  { icon: Lock, text: 'Prepaid charging credit with per-session holds — no surprise bills' },
  { icon: PowerOff, text: 'Charging stops automatically on faults' },
];

const SafetyStrip = () => (
  <section className="mkt-safety" aria-label="Safety">
    <ul className="container mkt-safety-list">
      {SAFETY_POINTS.map(({ icon: Icon, text }) => (
        <li key={text}>
          <Icon size={16} aria-hidden="true" />
          {text}
        </li>
      ))}
    </ul>
  </section>
);

/* ---- footer: real links only ------------------------------------------------ */

const MktFooter = () => {
  const host = cpoOrigin();
  return (
    <footer className="mkt-footer">
      <div className="container mkt-footer-grid">
        <div className="mkt-footer-brand">
          <span className="brand">
            <span className="brand-bolt">
              <Zap size={16} aria-hidden="true" />
            </span>
            AmpHive
          </span>
          <p className="text-3 text-sm">
            Shared EV charging on the plug points India already has.
          </p>
        </div>
        <nav aria-label="Drivers">
          <h3 className="mkt-footer-h">Drivers</h3>
          <ul>
            <li><Link to="/map">Find a charger</Link></li>
            <li><Link to="/credit">Charging credit</Link></li>
            <li><Link to="/activity">Activity</Link></li>
            <li><Link to="/terms">Charging credit terms</Link></li>
          </ul>
        </nav>
        <nav aria-label="Hosts">
          <h3 className="mkt-footer-h">Hosts</h3>
          <ul>
            <li><a href={`${host}/cpo/dashboard`}>Host console</a></li>
            <li><a href={`${host}/cpo`}>Become a host</a></li>
          </ul>
        </nav>
        <nav aria-label="Account">
          <h3 className="mkt-footer-h">Account</h3>
          <ul>
            <li><Link to="/login">Sign in</Link></li>
            <li><Link to="/signup">Create account</Link></li>
          </ul>
        </nav>
      </div>
    </footer>
  );
};

/* ---- page -------------------------------------------------------------------- */

const Marketing = () => {
  const location = useLocation();
  const deepLinkPlug = new URLSearchParams(location.search).get('plug');

  return (
    <main className="mkt">
      {deepLinkPlug && <DeepLinkBanner plugId={deepLinkPlug} />}
      <Hero />
      <HowItWorks />
      <ForDrivers />
      <ForHosts />
      <SafetyStrip />
      <MktFooter />
    </main>
  );
};

export default Marketing;
