import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import './styles/global.css';

import { AuthProvider } from './contexts/AuthContext';
import { WalletProvider } from './contexts/WalletContext';
import { SessionProvider } from './contexts/SessionContext';

const root = ReactDOM.createRoot(document.getElementById('root'));
root.render(
  <React.StrictMode>
    <AuthProvider>
      <WalletProvider>
        <SessionProvider>
          <App />
        </SessionProvider>
      </WalletProvider>
    </AuthProvider>
  </React.StrictMode>
);
