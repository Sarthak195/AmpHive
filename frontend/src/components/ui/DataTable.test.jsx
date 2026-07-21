/**
 * DataTable tests: skeleton rows while loading, ErrorState (with Retry)
 * distinct from EmptyState (zero rows), data rows through render/num
 * columns, collapse mode's td[data-label], row click + Enter keyboard path,
 * and the pagination footer ("x–y of total", Prev/Next paging + disabling).
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import DataTable from './DataTable';

const columns = [
  { key: 'name', label: 'Charger' },
  { key: 'energy', label: 'Energy', num: true, render: (r) => `${r.energy} kWh` },
];

const rows = [
  { id: 1, name: 'Bay A', energy: 1.2 },
  { id: 2, name: 'Bay B', energy: 3.4 },
];

describe('DataTable', () => {
  it('shows skeleton rows (not data, not empty copy) while loading', () => {
    const { container } = render(
      <DataTable columns={columns} rows={[]} keyField="id" loading emptyTitle="No chargers" />
    );
    expect(container.querySelector('.table-wrap')).toHaveAttribute('aria-busy', 'true');
    expect(container.querySelectorAll('.skeleton').length).toBeGreaterThan(0);
    expect(screen.queryByText('No chargers')).not.toBeInTheDocument();
    // Headers still render so the layout doesn't jump.
    expect(screen.getByText('Charger')).toBeInTheDocument();
  });

  it('renders ErrorState with a working Retry on error — never the empty state', () => {
    const onRetry = vi.fn();
    render(
      <DataTable
        columns={columns}
        rows={[]}
        keyField="id"
        error={new Error('boom')}
        onRetry={onRetry}
        emptyTitle="No chargers"
      />
    );
    expect(screen.getByText("Couldn't load this")).toBeInTheDocument();
    expect(screen.getByText('boom')).toBeInTheDocument();
    expect(screen.queryByText('No chargers')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('renders EmptyState only for a true zero-row result', () => {
    render(
      <DataTable
        columns={columns}
        rows={[]}
        keyField="id"
        emptyTitle="No chargers"
        emptyBody="Add a gateway to get started."
      />
    );
    expect(screen.getByText('No chargers')).toBeInTheDocument();
    expect(screen.getByText('Add a gateway to get started.')).toBeInTheDocument();
  });

  it('renders rows through render()/num columns', () => {
    render(<DataTable columns={columns} rows={rows} keyField="id" />);
    expect(screen.getByText('Bay A')).toBeInTheDocument();
    expect(screen.getByText('1.2 kWh')).toBeInTheDocument();
    expect(screen.getByText('3.4 kWh')).toHaveClass('cell-num');
  });

  it('collapse mode stamps td[data-label] for the mobile card view', () => {
    render(<DataTable columns={columns} rows={rows} keyField="id" collapse />);
    const table = screen.getByRole('table');
    expect(table).toHaveClass('table-collapse');
    expect(screen.getByText('Bay A')).toHaveAttribute('data-label', 'Charger');
    expect(screen.getByText('1.2 kWh')).toHaveAttribute('data-label', 'Energy');
  });

  it('row click and Enter both fire onRowClick', () => {
    const onRowClick = vi.fn();
    render(<DataTable columns={columns} rows={rows} keyField="id" onRowClick={onRowClick} />);

    const row = screen.getByText('Bay A').closest('tr');
    expect(row).toHaveClass('row-link');
    fireEvent.click(row);
    expect(onRowClick).toHaveBeenCalledWith(rows[0]);

    fireEvent.keyDown(row, { key: 'Enter' });
    expect(onRowClick).toHaveBeenCalledTimes(2);
  });

  it('pagination footer shows "x–y of total" and pages with Prev/Next', () => {
    const onPage = vi.fn();
    const { rerender } = render(
      <DataTable
        columns={columns}
        rows={rows}
        keyField="id"
        pagination={{ total: 42, offset: 0, limit: 10, onPage }}
      />
    );
    expect(screen.getByText('1–10 of 42')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Prev' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: 'Next' }));
    expect(onPage).toHaveBeenCalledWith(10);

    // Last page: Next disables, Prev steps back.
    rerender(
      <DataTable
        columns={columns}
        rows={rows}
        keyField="id"
        pagination={{ total: 42, offset: 40, limit: 10, onPage }}
      />
    );
    expect(screen.getByText('41–42 of 42')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Prev' }));
    expect(onPage).toHaveBeenCalledWith(30);
  });
});
