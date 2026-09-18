// The one data grid. Every table in the app is this component with its own
// columns, so sorting, server-side pagination, column visibility, row
// selection, loading skeletons and empty/error states behave identically
// everywhere.
//
// All paging and sorting is server-side (`manualPagination`, `manualSorting`):
// the grid shows one page the API returned and turns clicks into URL changes,
// which the page's query reads. The table holds no copy of the rows.

import {
  columnVisibilityFeature,
  createColumnHelper,
  type ColumnVisibilityState,
  type RowSelectionState,
  rowPaginationFeature,
  rowSelectionFeature,
  rowSortingFeature,
  type SortingState,
  tableFeatures,
  type Updater,
  useTable,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ChevronLeft, ChevronRight, ChevronsUpDown, Columns3 } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";

import { EmptyState, ErrorState } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form";
import { Checkbox, MenuCheckItem, MenuContent, MenuLabel, MenuRoot, MenuTrigger } from "@/components/ui/overlay";
import { Skeleton } from "@/components/ui/surface";
import { formatInt } from "@/lib/format";
import { cn } from "@/lib/utils";

export const gridFeatures = tableFeatures({
  rowSortingFeature,
  rowPaginationFeature,
  rowSelectionFeature,
  columnVisibilityFeature,
});

export type GridFeatures = typeof gridFeatures;

/** A column helper bound to the grid's features. */
export function columnsFor<T extends object>() {
  return createColumnHelper<GridFeatures, T>();
}

const PAGE_SIZES = [25, 50, 100, 200];

export interface ServerPaging {
  offset: number;
  limit: number;
  total: number;
  onChange: (offset: number, limit: number) => void;
}

export interface ServerSort {
  /** The sort key, which must be a column id. */
  sort: string;
  order: "asc" | "desc";
  onChange: (sort: string, order: "asc" | "desc") => void;
}

interface DataTableProps<T extends object> {
  /** Stable id: column visibility is remembered per table. */
  id: string;
  // Column defs from `columnsFor<T>().columns([...])`; typed loosely here
  // because each column keeps its own value type.
  columns: readonly unknown[];
  data: T[] | undefined;
  getRowId: (row: T) => string;
  loading?: boolean;
  error?: unknown;
  onRetry?: () => void;
  paging?: ServerPaging;
  sorting?: ServerSort;
  selection?: { selected: RowSelectionState; onChange: (next: RowSelectionState) => void; canSelect?: (row: T) => boolean };
  onRowClick?: (row: T) => void;
  /** Row id to highlight - the queue's keyboard cursor. */
  activeRowId?: string | null;
  empty?: ReactNode;
  toolbar?: ReactNode;
  caption: string;
  dense?: boolean;
}

const EMPTY: never[] = [];

export function DataTable<T extends object>(props: DataTableProps<T>) {
  const { id, data, loading, error, paging, sorting, selection, onRowClick, activeRowId } = props;
  const [visibility, setVisibility] = useStoredVisibility(id);

  const sortingState: SortingState = useMemo(
    () => (sorting ? [{ id: sorting.sort, desc: sorting.order === "desc" }] : []),
    [sorting],
  );

  const table = useTable({
    features: gridFeatures,
    // Each column keeps its own value type, which a single array type cannot
    // express; the helper that built them already checked each one.
    columns: props.columns as never,
    data: (data ?? EMPTY) as T[],
    getRowId: (row: T) => props.getRowId(row),
    manualSorting: true,
    manualPagination: true,
    rowCount: paging?.total ?? data?.length ?? 0,
    enableSortingRemoval: false,
    enableRowSelection: selection ? (row) => selection.canSelect?.(row.original as T) ?? true : false,
    state: {
      sorting: sortingState,
      rowSelection: selection?.selected ?? {},
      columnVisibility: visibility,
      pagination: { pageIndex: 0, pageSize: paging?.limit ?? 50 },
    },
    onSortingChange: (updater: Updater<SortingState>) => {
      if (!sorting) return;
      const next = typeof updater === "function" ? updater(sortingState) : updater;
      const first = next[0];
      if (first) sorting.onChange(first.id, first.desc ? "desc" : "asc");
    },
    onRowSelectionChange: (updater: Updater<RowSelectionState>) => {
      if (!selection) return;
      selection.onChange(typeof updater === "function" ? updater(selection.selected) : updater);
    },
    onColumnVisibilityChange: setVisibility,
  });

  const leafColumns = table.getAllLeafColumns();
  const visibleCount = table.getVisibleLeafColumns().length + (selection ? 1 : 0);
  const rows = table.getRowModel().rows;
  const allSelected = table.getIsAllPageRowsSelected();
  const someSelected = table.getIsSomePageRowsSelected() && !allSelected;

  return (
    <div className="overflow-hidden rounded-lg bg-surface ring-1 ring-border">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">{props.toolbar}</div>
        <MenuRoot>
          <MenuTrigger asChild>
            <Button size="sm" variant="ghost" aria-label="Choose columns">
              <Columns3 /> Columns
            </Button>
          </MenuTrigger>
          <MenuContent>
            <MenuLabel>Show columns</MenuLabel>
            {leafColumns
              .filter((column) => column.getCanHide())
              .map((column) => (
                <MenuCheckItem
                  key={column.id}
                  checked={column.getIsVisible()}
                  onCheckedChange={(value) => column.toggleVisibility(Boolean(value))}
                  onSelect={(event) => event.preventDefault()}
                >
                  {typeof column.columnDef.header === "string" ? column.columnDef.header : column.id}
                </MenuCheckItem>
              ))}
          </MenuContent>
        </MenuRoot>
      </div>

      <div className="overflow-x-auto">
        <table className={cn("w-full border-collapse text-sm", props.dense && "text-xs")}>
          <caption className="sr-only">{props.caption}</caption>
          <thead className="bg-surface-2 text-left text-xs text-muted">
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id}>
                {selection ? (
                  <th className="w-9 px-3 py-2">
                    <Checkbox
                      aria-label="Select every row on this page"
                      checked={someSelected ? "indeterminate" : allSelected}
                      onCheckedChange={(value) => table.toggleAllPageRowsSelected(Boolean(value))}
                    />
                  </th>
                ) : null}
                {group.headers.map((header) => {
                  const sortable = header.column.getCanSort() && sorting;
                  const dir = header.column.getIsSorted();
                  return (
                    <th
                      key={header.id}
                      scope="col"
                      className="px-3 py-2 font-medium whitespace-nowrap"
                      aria-sort={dir === "asc" ? "ascending" : dir === "desc" ? "descending" : undefined}
                    >
                      {header.isPlaceholder ? null : sortable ? (
                        <button
                          type="button"
                          className="inline-flex items-center gap-1 hover:text-ink"
                          onClick={header.column.getToggleSortingHandler()}
                        >
                          <table.FlexRender header={header} />
                          {dir === "asc" ? (
                            <ArrowUp className="size-3.5" aria-hidden />
                          ) : dir === "desc" ? (
                            <ArrowDown className="size-3.5" aria-hidden />
                          ) : (
                            <ChevronsUpDown className="size-3.5 opacity-50" aria-hidden />
                          )}
                        </button>
                      ) : (
                        <table.FlexRender header={header} />
                      )}
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>
          <tbody>
            {error ? (
              <tr>
                <td colSpan={visibleCount}>
                  <ErrorState error={error} onRetry={props.onRetry} />
                </td>
              </tr>
            ) : loading && !data ? (
              Array.from({ length: 8 }, (_, i) => (
                <tr key={i} className="border-t">
                  {Array.from({ length: visibleCount }, (_, j) => (
                    <td key={j} className="px-3 py-2.5">
                      <Skeleton className="h-4" style={{ width: `${40 + ((i * 7 + j * 13) % 50)}%` }} />
                    </td>
                  ))}
                </tr>
              ))
            ) : rows.length === 0 ? (
              <tr>
                <td colSpan={visibleCount}>{props.empty ?? <EmptyState title="Nothing matches these filters" />}</td>
              </tr>
            ) : (
              rows.map((row) => {
                const active = activeRowId != null && row.id === activeRowId;
                return (
                  <tr
                    key={row.id}
                    data-row-id={row.id}
                    aria-selected={selection ? row.getIsSelected() : undefined}
                    className={cn(
                      "border-t transition-colors",
                      onRowClick && "cursor-pointer hover:bg-surface-2",
                      row.getIsSelected() && "bg-accent-wash/40",
                      active && "outline-2 -outline-offset-2 outline-ring",
                      loading && "opacity-60",
                    )}
                    onClick={onRowClick ? () => onRowClick(row.original as T) : undefined}
                  >
                    {selection ? (
                      <td className="px-3 py-2" onClick={(event) => event.stopPropagation()}>
                        <Checkbox
                          aria-label="Select row"
                          checked={row.getIsSelected()}
                          disabled={!row.getCanSelect()}
                          onCheckedChange={(value) => row.toggleSelected(Boolean(value))}
                        />
                      </td>
                    ) : null}
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className={cn("px-3 align-middle", props.dense ? "py-1.5" : "py-2")}>
                        <table.FlexRender cell={cell} />
                      </td>
                    ))}
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {paging ? <Pager {...paging} count={rows.length} /> : null}
    </div>
  );
}

function Pager({ offset, limit, total, count, onChange }: ServerPaging & { count: number }) {
  const first = total === 0 ? 0 : offset + 1;
  const last = offset + count;
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t px-3 py-2 text-xs text-muted">
      <span className="tabular" aria-live="polite">
        {formatInt(first)}–{formatInt(last)} of {formatInt(total)}
      </span>
      <div className="flex items-center gap-2">
        <label className="flex items-center gap-1.5">
          Rows
          <Select
            className="w-20"
            value={String(limit)}
            onChange={(event) => onChange(0, Number(event.target.value))}
            aria-label="Rows per page"
          >
            {PAGE_SIZES.map((size) => (
              <option key={size} value={size}>
                {size}
              </option>
            ))}
          </Select>
        </label>
        <Button size="icon" variant="ghost" aria-label="Previous page" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit), limit)}>
          <ChevronLeft />
        </Button>
        <Button size="icon" variant="ghost" aria-label="Next page" disabled={offset + limit >= total} onClick={() => onChange(offset + limit, limit)}>
          <ChevronRight />
        </Button>
      </div>
    </div>
  );
}

function useStoredVisibility(id: string): [ColumnVisibilityState, (updater: Updater<ColumnVisibilityState>) => void] {
  const key = `concordance.columns.${id}`;
  const [state, setState] = useState<ColumnVisibilityState>(() => {
    try {
      return JSON.parse(localStorage.getItem(key) ?? "{}") as ColumnVisibilityState;
    } catch {
      return {};
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem(key, JSON.stringify(state));
    } catch {
      // Unavailable storage: the choice lasts for this page only.
    }
  }, [key, state]);
  return [state, (updater) => setState((prev) => (typeof updater === "function" ? updater(prev) : updater))];
}
