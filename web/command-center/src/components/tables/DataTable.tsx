export interface Column<T extends object> {
  key: string;
  label: string;
  render?: (row: T) => React.ReactNode;
  width?: string;
  align?: "left" | "right" | "center";
}

export function DataTable<T extends object>({
  rows,
  columns,
  empty = "No data returned.",
  caption = "Data table",
  getRowClassName,
  onRowClick,
}: {
  rows: T[];
  columns: Column<T>[];
  empty?: React.ReactNode;
  caption?: string;
  getRowClassName?: (row: T) => string;
  onRowClick?: (row: T) => void;
}) {
  return (
    <div className="table-wrap">
      <table>
        <caption className="sr-only">{caption}</caption>
        <colgroup>
          {columns.map((column) => (
            <col key={column.key} style={column.width ? { width: column.width } : undefined} />
          ))}
        </colgroup>
        <thead>
          <tr>
            {columns.map((column) => (
              <th key={column.key} className={column.align ? `align-${column.align}` : undefined}>{column.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length ? (
            rows.map((row, index) => (
              <tr key={rowKey(row, index)} className={[getRowClassName?.(row), onRowClick ? "row-clickable" : ""].filter(Boolean).join(" ")} onClick={onRowClick ? () => onRowClick(row) : undefined}>
                {columns.map((column) => {
                  const raw = (row as Record<string, unknown>)[column.key];
                  const title = typeof raw === "string" || typeof raw === "number" ? String(raw) : undefined;
                  return (
                    <td key={column.key} title={title} className={column.align ? `align-${column.align}` : undefined}>
                      {column.render ? column.render(row) : formatCell(raw)}
                    </td>
                  );
                })}
              </tr>
            ))
          ) : (
            <tr>
              <td className="empty-cell" colSpan={columns.length}>
                {typeof empty === "string" ? (
                  <>
                    <span className="empty-icon" aria-hidden="true">[]</span>
                    {empty}
                  </>
                ) : (
                  empty
                )}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </div>
  );
}

function rowKey(row: object, index: number): string {
  const record = row as Record<string, unknown>;
  return String(record.id ?? record.event_id ?? record.anomaly_id ?? record.incident_id ?? record.plan_id ?? record.run_id ?? record.investigation_id ?? record.execution_id ?? index);
}

function formatCell(value: unknown): React.ReactNode {
  if (value === undefined || value === null || value === "") return <span className="muted">-</span>;
  if (typeof value === "object") return <code>{JSON.stringify(value).slice(0, 160)}</code>;
  if (typeof value === "boolean") return value ? "yes" : "no";
  return String(value);
}
