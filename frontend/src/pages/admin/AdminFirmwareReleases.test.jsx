/**
 * AdminFirmwareReleases tests (feat/ota-version-picker): the registry table
 * (active by default, with a toggle to include deactivated releases),
 * registering a new release, and deactivating one via the named confirm
 * dialog.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

import AdminFirmwareReleases from './AdminFirmwareReleases';
import api from '../../api/client';

vi.mock('../../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), upload: vi.fn() },
}));

const toast = { ok: vi.fn(), error: vi.fn(), info: vi.fn(), warn: vi.fn() };
vi.mock('../../components/ui', async (importOriginal) => {
  const actual = await importOriginal();
  return { ...actual, useToast: () => toast };
});

const RELEASES = [
  {
    id: 2,
    version: '2.4.0-direct',
    url: 'https://storage.googleapis.com/amphive-fw/2.4.0.bin',
    notes: 'adds sub-16A cap enforcement',
    is_active: true,
    created_at: '2026-08-01T00:00:00Z',
  },
  {
    id: 1,
    version: '2.3.0',
    url: 'https://storage.googleapis.com/amphive-fw/2.3.0.bin',
    notes: null,
    is_active: true,
    created_at: '2026-07-01T00:00:00Z',
  },
];

const renderPage = () => render(<AdminFirmwareReleases />);

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ total: RELEASES.length, items: RELEASES });
});

describe('AdminFirmwareReleases', () => {
  it('renders the release table with version, url, and notes', async () => {
    renderPage();

    expect(await screen.findByText('2.4.0-direct')).toBeInTheDocument();
    expect(screen.getByText('2.3.0')).toBeInTheDocument();
    expect(screen.getByText('adds sub-16A cap enforcement')).toBeInTheDocument();
    expect(screen.getByText(/amphive-fw\/2\.4\.0\.bin/)).toBeInTheDocument();
  });

  it('fetches only active releases by default, and all when the toggle is checked', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2.4.0-direct');

    expect(api.get).toHaveBeenCalledWith('/api/admin/firmware-releases?active=true');

    await user.click(screen.getByLabelText('Show deactivated releases'));
    expect(api.get).toHaveBeenCalledWith('/api/admin/firmware-releases');
  });

  it('registers a new release and refreshes the list', async () => {
    api.post.mockResolvedValue({ id: 3, version: '2.5.0-direct' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2.4.0-direct');

    await user.click(screen.getByRole('button', { name: 'Register release' }));
    const modal = (await screen.findByLabelText('Version')).closest('.modal');
    await user.type(within(modal).getByLabelText('Version'), '2.5.0-direct');
    await user.type(
      within(modal).getByLabelText('Image URL (https)'),
      'https://storage.googleapis.com/amphive-fw/2.5.0.bin'
    );
    await user.type(within(modal).getByLabelText(/Notes/), 'test release');
    await user.click(within(modal).getByRole('button', { name: 'Register' }));

    expect(api.post).toHaveBeenCalledWith('/api/admin/firmware-releases', {
      version: '2.5.0-direct',
      url: 'https://storage.googleapis.com/amphive-fw/2.5.0.bin',
      notes: 'test release',
    });
    expect(toast.ok).toHaveBeenCalled();
  });

  it('surfaces a duplicate-version registration failure inline', async () => {
    api.post.mockRejectedValue(new Error("Firmware release '2.3.0' is already registered."));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2.4.0-direct');

    await user.click(screen.getByRole('button', { name: 'Register release' }));
    const modal = (await screen.findByLabelText('Version')).closest('.modal');
    await user.type(within(modal).getByLabelText('Version'), '2.3.0');
    await user.type(within(modal).getByLabelText('Image URL (https)'), 'https://storage.googleapis.com/amphive-fw/dup.bin');
    await user.click(within(modal).getByRole('button', { name: 'Register' }));

    expect(await screen.findByText("Firmware release '2.3.0' is already registered.")).toBeInTheDocument();
  });

  it('deactivates a release via the named confirm dialog', async () => {
    api.post.mockResolvedValue({ status: 'deactivated' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2.4.0-direct');

    const row = screen.getByText('2.4.0-direct').closest('tr');
    await user.click(within(row).getByRole('button', { name: 'Deactivate' }));

    const confirmModal = (await screen.findByText('Deactivate this release?')).closest('.modal');
    expect(within(confirmModal).getByText(/2\.4\.0-direct/)).toBeInTheDocument();
    await user.click(within(confirmModal).getByRole('button', { name: 'Deactivate' }));

    expect(api.post).toHaveBeenCalledWith('/api/admin/firmware-releases/2/deactivate');
    expect(toast.ok).toHaveBeenCalled();
  });

  it('uploads a firmware image (notes in the query string) and refreshes on success', async () => {
    api.upload.mockResolvedValue({ id: 5, version: '2.6.0-direct', size_bytes: 1048576, filename: 'amphive-gateway.bin' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2.4.0-direct');

    await user.click(screen.getByRole('button', { name: 'Upload image' }));
    const modal = (await screen.findByLabelText('Firmware image (.bin)')).closest('.modal');
    const file = new File([new Uint8Array([1, 2, 3])], 'amphive-gateway.bin', {
      type: 'application/octet-stream',
    });
    await user.upload(within(modal).getByLabelText('Firmware image (.bin)'), file);
    await user.type(within(modal).getByLabelText(/Notes/), 'nightly');
    await user.click(within(modal).getByRole('button', { name: 'Upload image' }));

    expect(api.upload).toHaveBeenCalledWith('/api/admin/firmware-releases/upload?notes=nightly', file);
    expect(toast.ok).toHaveBeenCalledWith('Registered 2.6.0-direct.');
  });

  it('uploads without notes (no query string)', async () => {
    api.upload.mockResolvedValue({ id: 6, version: '2.7.0-direct' });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2.4.0-direct');

    await user.click(screen.getByRole('button', { name: 'Upload image' }));
    const modal = (await screen.findByLabelText('Firmware image (.bin)')).closest('.modal');
    const file = new File([new Uint8Array([9])], 'amphive-gateway.bin', {
      type: 'application/octet-stream',
    });
    await user.upload(within(modal).getByLabelText('Firmware image (.bin)'), file);
    await user.click(within(modal).getByRole('button', { name: 'Upload image' }));

    expect(api.upload).toHaveBeenCalledWith('/api/admin/firmware-releases/upload', file);
  });

  it('surfaces an upload error detail inline (e.g. bad image)', async () => {
    api.upload.mockRejectedValue(new Error('not an ESP32 app image'));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText('2.4.0-direct');

    await user.click(screen.getByRole('button', { name: 'Upload image' }));
    const modal = (await screen.findByLabelText('Firmware image (.bin)')).closest('.modal');
    const file = new File([new Uint8Array([0])], 'notfirmware.bin', {
      type: 'application/octet-stream',
    });
    await user.upload(within(modal).getByLabelText('Firmware image (.bin)'), file);
    await user.click(within(modal).getByRole('button', { name: 'Upload image' }));

    expect(await screen.findByText('not an ESP32 app image')).toBeInTheDocument();
    expect(toast.ok).not.toHaveBeenCalled(); // no success toast on failure
  });

  it('shows EmptyState when there are no releases', async () => {
    api.get.mockResolvedValue({ total: 0, items: [] });
    renderPage();

    expect(await screen.findByText('No firmware releases yet')).toBeInTheDocument();
  });

  it('shows a retryable ErrorState on fetch failure', async () => {
    api.get.mockRejectedValueOnce(new Error('Network down'));
    const user = userEvent.setup();
    renderPage();

    expect(await screen.findByText("Couldn't load this")).toBeInTheDocument();
    api.get.mockResolvedValueOnce({ total: RELEASES.length, items: RELEASES });
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByText('2.4.0-direct')).toBeInTheDocument();
  });
});
