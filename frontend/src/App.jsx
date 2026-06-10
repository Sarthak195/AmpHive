import React from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import Navbar from './components/Navbar';
import Home from './pages/Home';
import TopUp from './pages/TopUp';
import Session from './pages/Session';

function App() {
  return (
    <Router>
      <div className="glass" style={{ minHeight: '100vh' }}>
        <Navbar />
        <div style={{ padding: '2rem' }}>
          <Routes>
            <Route path="/" element={<Home />} />
            <Route path="/topup" element={<TopUp />} />
            <Route path="/session" element={<Session />} />
          </Routes>
        </div>
      </div>
    </Router>
  );
}

export default App;
