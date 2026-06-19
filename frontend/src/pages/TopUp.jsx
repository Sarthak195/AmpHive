/**
 * AmpHive Top-Up Page — Razorpay Integration
 * ============================================
 * Displays coin balance and top-up options in INR.
 * Opens the Razorpay Standard Checkout modal for payment.
 *
 * Flow:
 * 1. User selects a top-up amount (₹50 / ₹100 / ₹200 / ₹500)
 * 2. Clicks "Pay" → frontend calls POST /api/payments/create-order
 * 3. Backend creates a Razorpay order and returns order_id
 * 4. Frontend opens the Razorpay Checkout modal with the order_id
 * 5. User pays via UPI, card, wallet, or net banking
 * 6. On success, Razorpay returns payment_id, order_id, signature
 * 7. Frontend calls POST /api/payments/verify with all three values
 * 8. Backend verifies HMAC SHA256 signature and credits coins
 * 9. Frontend refreshes the wallet balance
 */

import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useWallet } from '../contexts/WalletContext';
import { useAuth } from '../contexts/AuthContext';
import api from '../api/client';

// Razorpay Key ID from Vite environment variable (public, safe for frontend)
const RAZORPAY_KEY_ID = import.meta.env.VITE_RAZORPAY_KEY_ID || '';

const TopUp = () => {
  const { balance, refreshBalance } = useWallet();
  const { user } = useAuth();
  const navigate = useNavigate();

  const [isProcessing, setIsProcessing] = useState(false);
  const [selectedAmount, setSelectedAmount] = useState(100);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  if (!user) {
    return (
      <div className="page-container text-center animate-fade-in">
        <h2>Sign in to top up</h2>
        <p>You need an account to add coins to your wallet.</p>
        <button className="btn btn-primary mt-4" onClick={() => navigate('/login')}>Sign In</button>
      </div>
    );
  }

  // Top-up options with INR pricing (1:1 coin mapping)
  const options = [
    { coins: 50, price: '₹50', amountInr: 50 },
    { coins: 100, price: '₹100', amountInr: 100 },
    { coins: 200, price: '₹200', amountInr: 200 },
    { coins: 500, price: '₹500', amountInr: 500 },
  ];

  /**
   * Handle the payment flow:
   * 1. Create Razorpay order via backend
   * 2. Open Razorpay Checkout modal
   * 3. Verify payment signature via backend
   * 4. Refresh wallet balance
   */
  const handlePayment = async () => {
    setError('');
    setSuccess('');
    setIsProcessing(true);

    try {
      // Step 1: Create order on the backend
      const orderData = await api.post('/api/payments/create-order', {
        amount_inr: selectedAmount,
      });

      // Step 2: Open Razorpay Checkout modal
      const razorpayOptions = {
        key: RAZORPAY_KEY_ID,
        amount: orderData.amount,         // Amount in paise (from backend)
        currency: orderData.currency,     // "INR"
        order_id: orderData.order_id,     // Razorpay order ID
        name: 'AmpHive',
        description: `Wallet Top-Up: ${selectedAmount} Coins`,
        image: '', // Can add AmpHive logo URL here
        prefill: {
          email: user.email || '',
          name: user.full_name || '',
        },
        theme: {
          color: '#38b2f0',               // Match --color-primary
          backdrop_color: 'rgba(15, 20, 25, 0.85)',
        },
        modal: {
          ondismiss: () => {
            // User closed the modal without completing payment
            setIsProcessing(false);
            setError('Payment was cancelled.');
          },
        },
        // Step 3: On successful payment
        handler: async function (response) {
          try {
            // Verify the payment signature on the backend
            const verifyResult = await api.post('/api/payments/verify', {
              razorpay_order_id: response.razorpay_order_id,
              razorpay_payment_id: response.razorpay_payment_id,
              razorpay_signature: response.razorpay_signature,
              amount_inr: selectedAmount,
            });

            // Step 4: Refresh wallet balance and show success
            await refreshBalance();
            setSuccess(`✓ ${verifyResult.coins_credited} coins added! New balance: ${verifyResult.new_balance}`);
          } catch (verifyErr) {
            setError(`Payment received but verification failed: ${verifyErr.message}. Contact support.`);
          } finally {
            setIsProcessing(false);
          }
        },
      };

      // Check if Razorpay SDK is loaded
      if (typeof window.Razorpay === 'undefined') {
        setError('Razorpay SDK not loaded. Please refresh the page and try again.');
        setIsProcessing(false);
        return;
      }

      const rzp = new window.Razorpay(razorpayOptions);

      // Handle payment failure events
      rzp.on('payment.failed', function (response) {
        setError(`Payment failed: ${response.error.description || 'Unknown error'}. Please try again.`);
        setIsProcessing(false);
      });

      rzp.open();

    } catch (err) {
      setError(err.message || 'Failed to create payment order. Please try again.');
      setIsProcessing(false);
    }
  };

  return (
    <div className="page-container animate-fade-in">
      <header style={{ marginBottom: '2rem' }}>
        <h1 style={{ marginBottom: '0.25rem' }}>Top Up Wallet</h1>
        <p>
          Current Balance:{' '}
          <strong style={{ color: 'var(--color-accent)', fontSize: '1.15rem' }}>
            {balance} Coins
          </strong>
        </p>
      </header>

      <div className="glass glass-panel animate-slide-up flex flex-col gap-6">
        <h3>Select Amount</h3>

        {/* Amount selection grid */}
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '1rem' }}>
          {options.map(opt => (
            <div
              key={opt.coins}
              onClick={() => { setSelectedAmount(opt.amountInr); setError(''); setSuccess(''); }}
              className="glass-card"
              style={{
                padding: '1.5rem',
                border: `2px solid ${selectedAmount === opt.amountInr
                  ? 'var(--color-primary)'
                  : 'var(--color-surface-border)'}`,
                background: selectedAmount === opt.amountInr
                  ? 'var(--color-primary-glow)'
                  : 'transparent',
                textAlign: 'center',
                cursor: 'pointer',
              }}
            >
              <div style={{
                fontSize: '1.75rem',
                fontWeight: 700,
                color: 'var(--color-accent)',
                marginBottom: '0.25rem',
              }}>
                {opt.coins}
              </div>
              <div style={{ color: 'var(--color-text-secondary)', fontSize: '0.85rem', marginBottom: '0.5rem' }}>
                Coins
              </div>
              <div style={{
                fontWeight: 600,
                fontSize: '1.1rem',
                color: 'var(--color-text-primary)',
              }}>
                {opt.price}
              </div>
            </div>
          ))}
        </div>

        {/* Error / Success messages */}
        {error && (
          <div style={{
            padding: '0.75rem 1rem',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--color-danger-glow)',
            color: 'var(--color-danger)',
            fontSize: '0.9rem',
            fontWeight: 500,
          }}>
            {error}
          </div>
        )}
        {success && (
          <div style={{
            padding: '0.75rem 1rem',
            borderRadius: 'var(--radius-sm)',
            background: 'var(--color-success-glow)',
            color: 'var(--color-success)',
            fontSize: '0.9rem',
            fontWeight: 600,
          }}>
            {success}
          </div>
        )}

        {/* Pay button */}
        <div style={{ borderTop: '1px solid var(--color-surface-border)', paddingTop: '1.5rem' }}>
          <button
            className="btn btn-primary btn-lg btn-full"
            onClick={handlePayment}
            disabled={isProcessing}
          >
            {isProcessing
              ? 'Processing...'
              : `Pay ${options.find(o => o.amountInr === selectedAmount)?.price}`
            }
          </button>
          <p style={{
            textAlign: 'center',
            marginTop: '1rem',
            fontSize: '0.8rem',
            color: 'var(--color-text-muted)',
          }}>
            Secured by Razorpay • UPI, Cards, Wallets & Net Banking
          </p>
        </div>
      </div>
    </div>
  );
};

export default TopUp;
