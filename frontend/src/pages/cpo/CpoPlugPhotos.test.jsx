/**
 * CpoPlugPhotos tests: thumbnails load through the gated image route with the
 * Bearer token (an <img> can't send it), rejected rows show a "Removed"
 * placeholder instead of a broken image (their bytes are deleted), and an
 * approved photo can still be rejected (takedown) but not re-approved.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import CpoPlugPhotos from './CpoPlugPhotos';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}));

vi.mock('../../components/CpoLayout', () => ({
  default: ({ children }) => <div>{children}</div>,
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const APPROVED = { id: 1, plug_id: 5, url: '/api/plugs/5/photos/1/image', status: 'approved', created_at: null };
const REJECTED = { id: 2, plug_id: 5, url: '/api/plugs/5/photos/2/image', status: 'rejected', created_at: null };

describe('CpoPlugPhotos', () => {
  const originalFetch = global.fetch;
  const originalCreate = URL.createObjectURL;
  const originalRevoke = URL.revokeObjectURL;

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.setItem('amphive_token', 'tok');
    global.fetch = vi.fn(async () => ({ ok: true, blob: async () => new Blob(['x']) }));
    URL.createObjectURL = vi.fn(() => 'blob:photo');
    URL.revokeObjectURL = vi.fn();
    api.get.mockImplementation(async (url) =>
      url.startsWith('/api/cpo/plug-photos') ? [APPROVED, REJECTED] : [],
    );
    api.post.mockResolvedValue({});
  });

  afterEach(() => {
    global.fetch = originalFetch;
    URL.createObjectURL = originalCreate;
    URL.revokeObjectURL = originalRevoke;
    localStorage.clear();
  });

  it('loads thumbnails with the token and shows a placeholder for rejected photos', async () => {
    render(<CpoPlugPhotos />);

    const img = await screen.findByRole('img', { name: 'Charger #5' });
    expect(img).toHaveAttribute('src', 'blob:photo');
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch).toHaveBeenCalledWith('/api/plugs/5/photos/1/image', {
      headers: { Authorization: 'Bearer tok' },
    });
    expect(screen.getByText('Removed')).toBeInTheDocument();
    expect(screen.getAllByRole('img', { name: 'Charger #5' })).toHaveLength(1);
  });

  it('lets an approved photo be rejected but not approved again', async () => {
    render(<CpoPlugPhotos />);

    await screen.findByRole('img', { name: 'Charger #5' });
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Reject' }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/cpo/plug-photos/1/reject'),
    );
  });
});
