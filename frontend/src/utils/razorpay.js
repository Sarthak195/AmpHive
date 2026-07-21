/**
 * On-demand Razorpay Checkout loader.
 *
 * The SDK <script> used to sit in index.html and load on every page for
 * every visitor. Now the Wallet page calls loadRazorpay() right before
 * opening checkout: the script is injected once, cached, and the promise
 * resolves with window.Razorpay. CSP note: https://*.razorpay.com script-src
 * is already allowed in the deploy config.
 */

const RAZORPAY_SRC = 'https://checkout.razorpay.com/v1/checkout.js';

let razorpayPromise = null;

export function loadRazorpay() {
  if (window.Razorpay) return Promise.resolve(window.Razorpay);
  if (razorpayPromise) return razorpayPromise;

  razorpayPromise = new Promise((resolve, reject) => {
    const script = document.createElement('script');
    script.src = RAZORPAY_SRC;
    script.async = true;
    script.onload = () => resolve(window.Razorpay);
    script.onerror = () => {
      // Allow a retry on the next call instead of caching the failure.
      razorpayPromise = null;
      script.remove();
      reject(new Error("Couldn't load the payment window. Check your connection and try again."));
    };
    document.head.appendChild(script);
  });

  return razorpayPromise;
}

export default loadRazorpay;
