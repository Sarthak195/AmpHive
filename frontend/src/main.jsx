import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
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
