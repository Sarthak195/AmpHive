/**
 * ErrorBoundary — catches render-time errors anywhere below it and shows a
 * friendly reload prompt instead of a white screen.
 */

import { Component } from 'react';
import { TriangleAlert } from 'lucide-react';

class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Surfaced for debugging only — the fallback UI is the user-facing part.
    console.error('Unhandled render error:', error, info?.componentStack);
  }

  render() {
    if (this.state.error) {
      return (
        <main className="page">
          <div className="state-block">
            <TriangleAlert className="state-icon" aria-hidden="true" />
            <h1>Something went wrong</h1>
            <p>The app hit an unexpected error. Reloading usually fixes it.</p>
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => window.location.reload()}
            >
              Reload
            </button>
          </div>
        </main>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
