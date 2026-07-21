import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
// Self-hosted fonts (@fontsource) — prod CSP blocks font CDNs.
// Inter carries UI text, Bricolage Grotesque display headings, JetBrains
// Mono money/telemetry (see styles/tokens.css).
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import '@fontsource/bricolage-grotesque/600.css';
import '@fontsource/bricolage-grotesque/700.css';
import '@fontsource/bricolage-grotesque/800.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/700.css';
// Design system v3 (replaces global.css — old pages restyle per phase).
import './styles/tokens.css';
import './styles/base.css';
import './styles/primitives.css';
import './styles/layouts.css';

import ErrorBoundary from './components/ErrorBoundary';
import { ToastProvider } from './components/ui';
import { ConfigProvider } from './contexts/ConfigContext';
import { AuthProvider } from './contexts/AuthContext';
import { WalletProvider } from './contexts/WalletContext';
import { SessionProvider } from './contexts/SessionContext';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <ErrorBoundary>
      <ToastProvider>
        <ConfigProvider>
          <AuthProvider>
            <WalletProvider>
              <SessionProvider>
                <App />
              </SessionProvider>
            </WalletProvider>
          </AuthProvider>
        </ConfigProvider>
      </ToastProvider>
    </ErrorBoundary>
  </React.StrictMode>
);
