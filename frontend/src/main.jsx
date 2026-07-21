import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
// Inter self-hosted (@fontsource) — replaces the fonts.googleapis.com
// @import so the CSP can drop the Google Fonts origins entirely. Same
// weights the old css2 URL requested.
import '@fontsource/inter/300.css';
import '@fontsource/inter/400.css';
import '@fontsource/inter/500.css';
import '@fontsource/inter/600.css';
import '@fontsource/inter/700.css';
import './styles/global.css';

import { ConfigProvider } from './contexts/ConfigContext';
import { AuthProvider } from './contexts/AuthContext';
import { WalletProvider } from './contexts/WalletContext';
import { SessionProvider } from './contexts/SessionContext';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <ConfigProvider>
      <AuthProvider>
        <WalletProvider>
          <SessionProvider>
            <App />
          </SessionProvider>
        </WalletProvider>
      </AuthProvider>
    </ConfigProvider>
  </React.StrictMode>
);
