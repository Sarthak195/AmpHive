/**
 * AmpHive Login Page
 * ==================
 * Combined login/register form with glassmorphic styling.
 * Toggles between "Sign In" and "Create Account" modes.
 * On success, redirects to the Home page.
 */

import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

const Login = () => {
  const { login, register } = useAuth();
  const navigate = useNavigate();

  const [isRegister, setIsRegister] = useState(false);
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [fullName, setFullName] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      if (isRegister) {
        if (!fullName.trim()) {
          setError('Please enter your full name.');
          setLoading(false);
          return;
        }
        await register(email, password, fullName);
      } else {
        await login(email, password);
      }
      navigate('/');
    } catch (err) {
      setError(err.message || 'Something went wrong. Please try again.');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="page-container animate-fade-in" style={{ maxWidth: '440px', marginTop: '4rem' }}>
      {/* Logo Header */}
      <div className="text-center" style={{ marginBottom: '2.5rem' }}>
        <div style={{
          fontSize: '3rem',
          marginBottom: '0.5rem',
          filter: 'drop-shadow(0 0 20px var(--color-primary-glow))',
        }}>
          ⚡
        </div>
        <h1 style={{ color: 'var(--color-primary)', fontSize: '2.2rem', marginBottom: '0.25rem' }}>
          AmpHive
        </h1>
        <p style={{ fontSize: '1rem' }}>Shared EV Charging Network</p>
      </div>

      {/* Auth Form */}
      <div className="glass glass-panel animate-slide-up">
        <h2 style={{ marginBottom: '1.5rem', textAlign: 'center' }}>
          {isRegister ? 'Create Account' : 'Sign In'}
        </h2>

        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {/* Full Name (register only) */}
          {isRegister && (
            <div className="input-group">
              <label htmlFor="fullName">Full Name</label>
              <input
                id="fullName"
                type="text"
                className="input"
                placeholder="Enter your full name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
                autoComplete="name"
              />
            </div>
          )}

          {/* Email */}
          <div className="input-group">
            <label htmlFor="email">Email Address</label>
            <input
              id="email"
              type="email"
              className="input"
              placeholder="you@example.com"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              autoComplete="email"
            />
          </div>

          {/* Password */}
          <div className="input-group">
            <label htmlFor="password">Password</label>
            <input
              id="password"
              type="password"
              className="input"
              placeholder="Enter your password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={6}
              autoComplete={isRegister ? 'new-password' : 'current-password'}
            />
          </div>

          {/* Error Message */}
          {error && (
            <div className="error-text" style={{ textAlign: 'center', padding: '0.5rem' }}>
              {error}
            </div>
          )}

          {/* Submit Button */}
          <button
            type="submit"
            className="btn btn-primary btn-lg btn-full"
            disabled={loading}
            style={{ marginTop: '0.5rem' }}
          >
            {loading ? 'Please wait...' : (isRegister ? 'Create Account' : 'Sign In')}
          </button>
        </form>

        {/* Toggle between login/register */}
        <div className="divider" />
        <p style={{ textAlign: 'center', fontSize: '0.95rem' }}>
          {isRegister ? 'Already have an account?' : "Don't have an account?"}{' '}
          <button
            onClick={() => { setIsRegister(!isRegister); setError(''); }}
            style={{
              background: 'none',
              border: 'none',
              color: 'var(--color-primary)',
              cursor: 'pointer',
              fontWeight: 600,
              fontSize: '0.95rem',
              fontFamily: 'var(--font-family)',
            }}
          >
            {isRegister ? 'Sign In' : 'Create Account'}
          </button>
        </p>
      </div>
    </div>
  );
};

export default Login;
