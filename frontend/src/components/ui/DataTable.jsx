/**
 * DataTable — the uniform list surface for every table in the app.
 *
 *   columns   [{ key, label, num?, render?(row) }] — num right-aligns via
 *             .cell-num (tabular figures); render overrides row[key].
 *   rows      array of data objects; keyField picks the React key ('id').
 *   loading   shimmer skeleton rows (never a spinner).
 *   error     ErrorState with Retry (onRetry) — never conflated with...
 *   empty*    ...EmptyState (emptyIcon/emptyTitle/emptyBody/emptyAction),
 *             which renders only for a true zero-row result.
 *   onRowClick clickable rows (.row-link) with a keyboard path (Enter).
 *   pagination { total, offset, limit, onPage } → Prev/Next footer with
 *             "x–y of total".
 *   collapse  .table-collapse + td[data-label] → stacked cards <720px.
 */

import EmptyState from './EmptyState';
import ErrorState from './ErrorState';

function HeaderRow({ columns }) {
  return (
    <thead>
      <tr>
        {columns.map((c) => (
          <th key={c.key} scope="col" className={c.num ? 'cell-num' : undefined}>
            {c.label}
          </th>
        ))}
      </tr>
    </thead>
  );
}

function Pagination({ total, offset, limit, onPage }) {
  const start = total === 0 ? 0 : offset + 1;
  const end = Math.min(offset + limit, total);
  return (
    <div className="pagination">
      <span className="num" aria-live="polite">
        {start}–{end} of {total}
      </span>
      <div className="row">
        <button
          type="button"
          className="btn btn-quiet btn-sm"
          disabled={offset <= 0}
          onClick={() => onPage(Math.max(0, offset - limit))}
        >
          Prev
        </button>
        <button
          type="button"
          className="btn btn-quiet btn-sm"
          disabled={end >= total}
          onClick={() => onPage(offset + limit)}
        >
          Next
        </button>
      </div>
    </div>
  );
}

export default function DataTable({
  columns,
  rows,
  keyField = 'id',
  loading = false,
  error = null,
  onRetry,
  emptyIcon,
  emptyTitle = 'Nothing here yet',
  emptyBody,
  emptyAction,
  onRowClick,
  pagination,
  collapse = false,
}) {
  const tableClass = `table${collapse ? ' table-collapse' : ''}`;

  if (loading) {
    const skeletonRows = Math.min(pagination?.limit || 5, 8);
    return (
      <div className="table-wrap" aria-busy="true">
        <table className={tableClass}>
          <HeaderRow columns={columns} />
          <tbody>
            {Array.from({ length: skeletonRows }, (_, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c.key} data-label={collapse ? c.label : undefined}>
                    <div className="skeleton skeleton-text" aria-hidden="true" />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  if (error) {
    return (
      <div className="table-wrap">
        <ErrorState error={error} onRetry={onRetry} />
      </div>
    );
  }

  if (!rows || rows.length === 0) {
    return (
      <div className="table-wrap">
        <EmptyState icon={emptyIcon} title={emptyTitle} body={emptyBody} action={emptyAction} />
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table className={tableClass}>
        <HeaderRow columns={columns} />
        <tbody>
          {rows.map((row) => (
            <tr
              key={row[keyField]}
              className={onRowClick ? 'row-link' : undefined}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              onKeyDown={
                onRowClick
                  ? (e) => {
                      if (e.key === 'Enter') onRowClick(row);
                    }
                  : undefined
              }
              tabIndex={onRowClick ? 0 : undefined}
            >
              {columns.map((c) => (
                <td
                  key={c.key}
                  className={c.num ? 'cell-num' : undefined}
                  data-label={collapse ? c.label : undefined}
                >
                  {c.render ? c.render(row) : row[c.key]}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {pagination && <Pagination {...pagination} />}
    </div>
  );
}
