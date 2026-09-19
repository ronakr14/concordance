import { ChartColumn, Table2 } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";

import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui/surface";

/**
 * The chart tokens, resolved to colours. Recharts writes SVG attributes, so it
 * needs concrete values; this re-reads them when the theme class flips, so a
 * chart repaints with its dark-mode steps rather than keeping light ones.
 */
export function useChartTheme() {
  const read = () => {
    const style = getComputedStyle(document.documentElement);
    const v = (name: string) => style.getPropertyValue(name).trim();
    return {
      series1: v("--series-1"),
      series2: v("--series-2"),
      series3: v("--series-3"),
      series4: v("--series-4"),
      grid: v("--grid"),
      axis: v("--axis"),
      muted: v("--muted"),
      ink: v("--ink"),
      ink2: v("--ink-2"),
      surface: v("--surface"),
      hover: v("--surface-2"),
    };
  };
  const [theme, setTheme] = useState(read);
  useEffect(() => {
    const observer = new MutationObserver(() => setTheme(read()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);
  return theme;
}

/**
 * A chart in a card, with the states every chart needs: loading, error,
 * empty, and a table view of the same numbers - so a value is never available
 * only as a colour or a bar length.
 */
export function ChartCard({
  title,
  description,
  loading,
  error,
  onRetry,
  empty,
  chart,
  table,
  label,
  height = 240,
  actions,
}: {
  title: string;
  description?: string;
  loading: boolean;
  error: unknown;
  onRetry?: () => void;
  empty: boolean;
  chart: ReactNode;
  table: ReactNode;
  /** What the chart shows, for screen readers. */
  label: string;
  height?: number;
  actions?: ReactNode;
}) {
  const [asTable, setAsTable] = useState(false);
  return (
    <Card>
      <CardHeader
        title={title}
        description={description}
        actions={
          <>
            {actions}
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setAsTable((v) => !v)}
              aria-pressed={asTable}
              aria-label={asTable ? "Show as chart" : "Show as table"}
            >
              {asTable ? <ChartColumn /> : <Table2 />}
            </Button>
          </>
        }
      />
      <CardBody>
        {error ? (
          <ErrorState error={error} onRetry={onRetry} />
        ) : loading ? (
          <Skeleton style={{ height }} />
        ) : empty ? (
          <div style={{ minHeight: height }} className="grid place-items-center">
            <EmptyState title="No data yet" body="Run a reconciliation and this fills in." />
          </div>
        ) : asTable ? (
          <div className="overflow-auto" style={{ maxHeight: height + 40 }}>
            {table}
          </div>
        ) : (
          <figure role="img" aria-label={label} style={{ height }}>
            {chart}
          </figure>
        )}
      </CardBody>
    </Card>
  );
}

export function MiniTable({ head, rows }: { head: string[]; rows: (string | number)[][] }) {
  return (
    <table className="w-full text-sm">
      <thead className="text-left text-xs text-muted">
        <tr>
          {head.map((h, i) => (
            <th key={h} scope="col" className={i ? "px-2 py-1 text-right font-medium" : "px-2 py-1 font-medium"}>
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row, r) => (
          <tr key={r} className="border-t">
            {row.map((cell, i) => (
              <td key={i} className={i ? "tabular px-2 py-1 text-right" : "px-2 py-1"}>
                {cell}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The one tooltip look for every chart: surface card, ink text, series key beside the value. */
export function ChartTooltip({
  active,
  label,
  items,
}: {
  active?: boolean;
  label?: ReactNode;
  items: { name: string; value: string; color: string }[];
}) {
  if (!active || items.length === 0) return null;
  return (
    <div className="rounded-md bg-surface px-3 py-2 text-xs shadow-lg ring-1 ring-border">
      {label ? <p className="mb-1 font-medium text-ink">{label}</p> : null}
      {items.map((item) => (
        <p key={item.name} className="flex items-center gap-2 text-ink-2">
          <span className="size-2 rounded-full" style={{ background: item.color }} aria-hidden />
          {item.name}
          <span className="tabular ml-auto pl-3 font-medium text-ink">{item.value}</span>
        </p>
      ))}
    </div>
  );
}
