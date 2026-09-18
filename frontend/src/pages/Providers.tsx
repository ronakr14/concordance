import { useNavigate } from "react-router";

import { type ProviderFilters, useFacets, useProviders } from "@/api/queries";
import type { ProviderRow } from "@/api/types";
import { columnsFor, DataTable } from "@/components/DataTable";
import { FilterBar, FilterField, SearchInput } from "@/components/FilterBar";
import { EmptyState, PageHeader } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { Select } from "@/components/ui/form";
import { RECORD_TYPE } from "@/lib/status";
import { opt, useSearchState } from "@/lib/useSearchState";

import { providerName } from "./investigation/fields";

const DEFAULTS = {
  q: "",
  state: "",
  record_type: "",
  compliance: "",
  sort: "provider_id",
  order: "asc",
  offset: "0",
  limit: "50",
};

const col = columnsFor<ProviderRow>();
const COLUMNS = col.columns([
  col.accessor("provider_id", {
    header: "Provider ID",
    enableHiding: false,
    cell: (c) => <span className="font-mono text-xs">{c.getValue()}</span>,
  }),
  col.accessor("npi", { header: "NPI", enableSorting: false, cell: (c) => <span className="font-mono text-xs">{c.getValue() ?? "—"}</span> }),
  col.display({
    id: "name",
    header: "Name",
    cell: ({ row }) => {
      const type = RECORD_TYPE[row.original.is_organization ? "organization" : "individual"];
      return (
        <span className="inline-flex items-center gap-2">
          <type.icon className="size-3.5 shrink-0 text-muted" aria-label={type.label} />
          <span className="font-medium text-ink">{providerName(row.original)}</span>
        </span>
      );
    },
  }),
  col.accessor("specialty", { header: "Specialty", enableSorting: false, cell: (c) => c.getValue() ?? "—" }),
  col.accessor("organization_name", {
    header: "Organization",
    enableSorting: false,
    cell: ({ row }) => (row.original.is_organization ? row.original.dba_name ?? "—" : row.original.organization_name ?? "—"),
  }),
  col.display({
    id: "location",
    header: "Location",
    cell: ({ row }) => [row.original.city, row.original.state].filter(Boolean).join(", ") || "—",
  }),
  col.accessor("state", { header: "State", cell: (c) => c.getValue() ?? "—" }),
  col.accessor("compliance_status", {
    header: "Compliance",
    enableSorting: false,
    cell: (c) => <StatusBadge status={c.getValue()} />,
  }),
]);

export function ProvidersPage() {
  const navigate = useNavigate();
  const facets = useFacets();
  const { values: f, set, reset, active } = useSearchState(DEFAULTS);
  const filters: ProviderFilters = {
    q: opt(f.q),
    state: opt(f.state),
    record_type: opt(f.record_type) as ProviderFilters["record_type"],
    compliance: opt(f.compliance) as ProviderFilters["compliance"],
    sort: f.sort as ProviderFilters["sort"],
    order: f.order as ProviderFilters["order"],
    offset: Number(f.offset),
    limit: Number(f.limit),
  };
  const providers = useProviders(filters);

  return (
    <>
      <PageHeader title="Providers" description="The provider master file, each provider beside its compliance status - derived live from cases and pending results." />
      <FilterBar active={active} onReset={reset}>
        <FilterField label="Search" htmlFor="p-q" width="w-72">
          <SearchInput id="p-q" value={f.q} onChange={(q) => set({ q })} placeholder="Name, provider ID or 10-digit NPI" />
        </FilterField>
        <FilterField label="Compliance" htmlFor="p-compliance" width="w-36">
          <Select id="p-compliance" value={f.compliance} onChange={(e) => set({ compliance: e.target.value })}>
            <option value="">Any</option>
            <option value="EXCLUDED">Excluded</option>
            <option value="UNDER_REVIEW">Under review</option>
            <option value="CLEAR">Clear</option>
          </Select>
        </FilterField>
        <FilterField label="Type" htmlFor="p-type" width="w-36">
          <Select id="p-type" value={f.record_type} onChange={(e) => set({ record_type: e.target.value })}>
            <option value="">Any</option>
            <option value="individual">Individual</option>
            <option value="organization">Organization</option>
          </Select>
        </FilterField>
        <FilterField label="State" htmlFor="p-state" width="w-24">
          <Select id="p-state" value={f.state} onChange={(e) => set({ state: e.target.value })}>
            <option value="">Any</option>
            {facets.data?.states.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </FilterField>
      </FilterBar>
      <DataTable
        id="providers"
        caption="Provider directory"
        columns={COLUMNS}
        data={providers.data?.items}
        loading={providers.isFetching}
        error={providers.error}
        onRetry={() => void providers.refetch()}
        getRowId={(row) => row.provider_id}
        onRowClick={(row) => navigate(`/providers/${encodeURIComponent(row.provider_id)}`)}
        paging={{
          offset: Number(f.offset),
          limit: Number(f.limit),
          total: providers.data?.total ?? 0,
          onChange: (offset, limit) => set({ offset: String(offset), limit: String(limit) }, { resetPage: false }),
        }}
        sorting={{ sort: f.sort, order: f.order as "asc" | "desc", onChange: (sort, order) => set({ sort, order }) }}
        empty={<EmptyState title="No provider matches" body="Try a shorter name, or clear a filter. Names match on their normalized form, so accents and punctuation don't matter." />}
      />
    </>
  );
}
