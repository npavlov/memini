import type { ReactElement, ReactNode } from "react";

export interface DataTableProps {
  readonly columns: readonly string[];
  /** The last column holds buttons; its heading is read out but not shown. */
  readonly withActions?: boolean;
  /** The rows: `tr` elements. */
  readonly children: ReactNode;
}

/** A table that scrolls sideways when a value is very long, so the page never does. */
export function DataTable({ columns, withActions = false, children }: DataTableProps): ReactElement {
  return (
    <div className="table-scroll">
      <table className="table">
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column} scope="col">
                {column}
              </th>
            ))}
            {withActions ? (
              <th scope="col">
                <span className="visually-hidden">Actions</span>
              </th>
            ) : null}
          </tr>
        </thead>
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}
