import { useNavigate } from "react-router";

import { type CaseFilters, useCases } from "@/api/queries";
import type { CaseRow } from "@/api/types";
import { columnsFor, DataTable } from "@/components/DataTable";
import { FilterBar, FilterField, SavedViews } from "@/components/FilterBar";
import { EmptyState, PageHeader } from "@/components/states";
import { ConflictTag, StatusBadge } from "@/components/status";
import { Input, Select } from "@/components/ui/form";
import { formatDate } from "@/lib/format";
import { casePhaseKey } from "@/lib/status";
import { opt, useSearchState } from "@/lib/useSearchState";

const DEFAULTS = { status: "", conflict: "", provider_id: "", date_from: "", date_to: "", offset: "0", limit: "50" };

const col = columnsFor<CaseRow>();
const COLUMNS = col.columns([
  col.accessor("case_number", {
    header: "Case",
    enableSorting: false,
    enableHiding: false,
    cell: (c) => <span className="font-mono text-xs font-medium text-ink">{c.getValue()}</span>,
  }),
  col.accessor("provider_name", {
    header: "Provider",
    enableSorting: false,
    cell: ({ row }) => (
      <div>
        <div className="text-ink">{row.original.provider_name ?? "—"}</div>
        <div className="font-mono text-xs text-muted">{row.original.provider_id}</div>
      </div>
    ),
  }),
  col.accessor("subject_name", {
    header: "Sanction",
    enableSorting: false,
    cell: ({ row }) => (
      <div>
        <div className="text-ink">{row.original.subject_name ?? "—"}</div>
        <div className="text-xs text-muted">
          {[row.original.sanction_type, row.original.source_authority].filter(Boolean).join(" · ") || "—"}
        </div>
      </div>
    ),
  }),
  col.accessor("start_date", { header: "Start", enableSorting: false, cell: (c) => <span className="tabular text-xs">{formatDate(c.getValue())}</span> }),
  col.accessor("end_date", { header: "End", enableSorting: false, cell: (c) => <span className="tabular text-xs">{formatDate(c.getValue())}</span> }),
  col.accessor("status", {
    header: "Status",
    enableSorting: false,
    cell: ({ row }) => (
      <span className="inline-flex items-center gap-1.5">
        <StatusBadge status={casePhaseKey(row.original.phase)} />
        {row.original.conflict_flag ? <ConflictTag /> : null}
      </span>
    ),
  }),
  col.accessor("created_by_email", {
    header: "Approved by",
    enableSorting: false,
    cell: (c) => <span className="text-xs text-ink-2">{c.getValue() ?? "—"}</span>,
  }),
]);

export function CasesPage() {
  const navigate = useNavigate();
  const { values: f, set, reset, active, query } = useSearchState(DEFAULTS);
  const filters: CaseFilters = {
    status: opt(f.status) as CaseFilters["status"],
    conflict: f.conflict === "true",
    provider_id: opt(f.provider_id),
    date_from: opt(f.date_from),
    date_to: opt(f.date_to),
    offset: Number(f.offset),
    limit: Number(f.limit),
  };
  const cases = useCases(filters);

  return (
    <>
      <PageHeader title="Cases" description="Compliance cases opened by approval. A case runs for a fixed window, then expires on schedule unless an admin closes it first." />
      <FilterBar active={active} onReset={reset}>
        <FilterField label="Status" htmlFor="c-status" width="w-36">
          <Select id="c-status" value={f.status} onChange={(e) => set({ status: e.target.value })}>
            <option value="">Any</option>
            <option value="PENDING">Pending start</option>
            <option value="ACTIVE">Active</option>
            <option value="EXPIRED">Expired</option>
            <option value="CLOSED">Closed</option>
            <option value="REJECTED">Rejected</option>
          </Select>
        </FilterField>
        <FilterField label="Conflicts" htmlFor="c-conflict" width="w-36">
          <Select id="c-conflict" value={f.conflict} onChange={(e) => set({ conflict: e.target.value })}>
            <option value="">Any</option>
            <option value="true">Flagged only</option>
          </Select>
        </FilterField>
        <FilterField label="Provider ID" htmlFor="c-provider" width="w-36">
          <Input id="c-provider" value={f.provider_id} onChange={(e) => set({ provider_id: e.target.value.trim() })} placeholder="P0001487" />
        </FilterField>
        <FilterField label="Started between" htmlFor="c-from" width="w-64">
          <div className="flex items-center gap-1">
            <Input id="c-from" type="date" value={f.date_from} onChange={(e) => set({ date_from: e.target.value })} aria-label="From date" />
            <Input type="date" value={f.date_to} onChange={(e) => set({ date_to: e.target.value })} aria-label="To date" />
          </div>
        </FilterField>
      </FilterBar>
      <DataTable
        id="cases"
        caption="Cases"
        columns={COLUMNS}
        data={cases.data?.items}
        loading={cases.isFetching}
        error={cases.error}
        onRetry={() => void cases.refetch()}
        getRowId={(row) => row.id}
        onRowClick={(row) => navigate(`/cases/${row.id}`)}
        paging={{
          offset: Number(f.offset),
          limit: Number(f.limit),
          total: cases.data?.total ?? 0,
          onChange: (offset, limit) => set({ offset: String(offset), limit: String(limit) }, { resetPage: false }),
        }}
        toolbar={
          <div className="ml-auto">
            <SavedViews storageKey="cases" query={query} onApply={(q) => navigate(`/cases?${q}`)} />
          </div>
        }
        empty={<EmptyState title="No cases here" body="Cases open when an admin approves a match in the review queue." />}
      />
    </>
  );
}
