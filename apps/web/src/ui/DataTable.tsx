import type { ReactNode } from "react";

export interface Column<T> {
  key: string;
  header: ReactNode;
  /** Numbers and amounts: right-aligned, mono. */
  numeric?: boolean;
  width?: number | string;
  render: (row: T) => ReactNode;
}

/**
 * A real `<table>` (section 5, Data table): sunken header row, 1px row rules,
 * amounts right-aligned in the mono figure style, selected row in brand tint.
 * Debit and Credit are separate numeric columns; the caller never colours a
 * debit. `caption` is required so the table is named for assistive tech.
 */
export function DataTable<T>({
  caption,
  columns,
  rows,
  rowKey,
  selectedKey,
  footer,
  testId,
}: {
  caption: string;
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T) => string;
  selectedKey?: string | null;
  /** A totals row, one cell per column. */
  footer?: readonly ReactNode[];
  testId?: string;
}) {
  return (
    <div className="ui-tablewrap">
      <table className="ui-table" data-testid={testId}>
        <caption className="ui-visually-hidden">{caption}</caption>
        <thead>
          <tr>
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={column.numeric ? "ui-num" : undefined}
                style={column.width !== undefined ? { width: column.width } : undefined}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const key = rowKey(row);
            return (
              <tr key={key} aria-selected={selectedKey === key ? true : undefined}>
                {columns.map((column) => (
                  <td key={column.key} className={column.numeric ? "ui-num" : undefined}>
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
        {footer ? (
          <tfoot>
            <tr>
              {footer.map((cell, index) => (
                <td
                  key={columns[index]?.key ?? index}
                  className={columns[index]?.numeric ? "ui-num" : undefined}
                >
                  {cell}
                </td>
              ))}
            </tr>
          </tfoot>
        ) : null}
      </table>
    </div>
  );
}
