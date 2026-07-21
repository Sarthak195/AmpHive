/**
 * BootSplash — full-viewport brand pulse shown while the auth session is
 * restoring and as the Suspense fallback for lazy route chunks.
 */

import { Zap } from 'lucide-react';
import './BootSplash.css';

const BootSplash = () => (
  <div className="boot-splash" role="status">
    <span className="brand-bolt boot-splash-bolt">
      <Zap size={22} aria-hidden="true" />
    </span>
    <span className="sr-only">Loading</span>
  </div>
);

export default BootSplash;
