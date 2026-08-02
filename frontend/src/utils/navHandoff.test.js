/**
 * navHandoff tests: the Google Maps directions deep-link builder.
 */
import { describe, it, expect } from 'vitest';
import { googleMapsDirUrl } from './navHandoff';

describe('googleMapsDirUrl', () => {
  it('builds a directions URL with the destination coordinates', () => {
    expect(googleMapsDirUrl(12.9, 77.6)).toBe(
      'https://www.google.com/maps/dir/?api=1&destination=12.9,77.6'
    );
  });

  it('handles negative coordinates', () => {
    expect(googleMapsDirUrl(-33.87, 151.21)).toBe(
      'https://www.google.com/maps/dir/?api=1&destination=-33.87,151.21'
    );
  });
});
