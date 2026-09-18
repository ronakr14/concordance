import { Play, Upload } from "lucide-react";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import { type SanctionFilters, useFacets, useSanctionFiles, useSanctions, useStartRun } from "@/api/queries";
import type { SanctionFile, SanctionRecord } from "@/api/types";
import { columnsFor, DataTable } from "@/components/DataTable";
import { FilterBar, FilterField, SearchInput } from "@/components/FilterBar";
import { EmptyState, PageHeader } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { Button, buttonVariants } from "@/components/ui/button";
import { Select } from "@/components/ui/form";
import { TabsContent, TabsList, TabsRoot, TabsTrigger } from "@/components/ui/overlay";
import { formatDate, formatDateTime, formatInt } from "@/lib/format";
import { RECORD_TYPE } from "@/lib/status";
import { opt, useSearchState } from "@/lib/useSearchState";

const DEFAULTS = {
  tab: "records",
  q: "",
  source_authority: "",
  state: "",
  sanction_type: "",
  record_type: "",
  history: "",
  offset: "0",
  limit: "50",
  foffset: "0",
};

const rcol = columnsFor<SanctionRecord>();
const RECORD_COLUMNS = rcol.columns([
  rcol.accessor("record_id", {
    header: "Record",
    enableSorting: false,
    enableHiding: false,
    cell: (c) => <span className="font-mono text-xs">{c.getValue()}</span>,
  }),
  rcol.display({
    id: "subject",
    header: "Subject",
    cell: ({ row }) => {
      const r = row.original;
      const type = RECORD_TYPE[r.is_organization ? "organization" : "individual"];
      const name = r.is_organization ? r.organization_name : [r.first_name, r.middle_name, r.last_name].filter(Boolean).join(" ");
      return (
        <span className="inline-flex items-center gap-2">
          <type.icon className="size-3.5 text-muted" aria-label={type.label} />
          <span className="font-medium text-ink">{name || "—"}</span>
        </span>
      );
    },
  }),
  rcol.accessor("npi", { header: "NPI", enableSorting: false, cell: (c) => <span className="font-mono text-xs">{c.getValue() ?? "—"}</span> }),
  rcol.accessor("sanction_type", { header: "Type", enableSorting: false, cell: (c) => c.getValue() ?? "—" }),
  rcol.accessor("exclusion_date", { header: "Excluded", enableSorting: false, cell: (c) => <span className="tabular text-xs">{formatDate(c.getValue())}</span> }),
  rcol.display({ id: "where", header: "Location", cell: ({ row }) => [row.original.city, row.original.state].filter(Boolean).join(", ") || "—" }),
  rcol.accessor("source_authority", { header: "Source", enableSorting: false, cell: (c) => c.getValue() ?? "—" }),
  rcol.accessor("is_current", {
    header: "Version",
    enableSorting: false,
    cell: (c) => (c.getValue() ? <span className="text-xs text-ink">Current</span> : <span className="text-xs text-muted">Replaced</span>),
  }),
]);

export function SanctionsPage() {
  const { values: f, set, reset, active } = useSearchState(DEFAULTS);
  return (
    <>
      <PageHeader
        title="Sanctions"
        description="Exclusion records as uploaded, and where each came from."
        actions={
          <Link to="/sanctions/upload" className={buttonVariants({ variant: "primary" })}>
            <Upload className="size-4" /> Upload a list
          </Link>
        }
      />
      <TabsRoot value={f.tab} onValueChange={(tab) => set({ tab }, { resetPage: false })}>
        <TabsList>
          <TabsTrigger value="records">Records</TabsTrigger>
          <TabsTrigger value="files">Files and lineage</TabsTrigger>
        </TabsList>
        <TabsContent value="records" className="pt-3">
          <Records f={f} set={set} reset={reset} active={active} />
        </TabsContent>
        <TabsContent value="files" className="pt-3">
          <Files offset={Number(f.foffset)} onOffset={(foffset) => set({ foffset: String(foffset) }, { resetPage: false })} />
        </TabsContent>
      </TabsRoot>
    </>
  );
}

function Records({
  f,
  set,
  reset,
  active,
}: {
  f: typeof DEFAULTS;
  set: (patch: Partial<typeof DEFAULTS>, options?: { resetPage?: boolean }) => void;
  reset: () => void;
  active: number;
}) {
  const facets = useFacets();
  const filters: SanctionFilters = {
    q: opt(f.q),
    source_authority: opt(f.source_authority),
    state: opt(f.state),
    sanction_type: opt(f.sanction_type),
    is_organization: f.record_type === "" ? undefined : f.record_type === "organization",
    include_history: f.history === "true",
    offset: Number(f.offset),
    limit: Number(f.limit),
  };
  const records = useSanctions(filters);
  return (
    <>
      <FilterBar active={Math.max(0, active - (f.tab !== "records" ? 1 : 0))} onReset={reset}>
        <FilterField label="Search" htmlFor="s-q" width="w-64">
          <SearchInput id="s-q" value={f.q} onChange={(q) => set({ q })} placeholder="Name, record key or NPI" />
        </FilterField>
        <FilterField label="Source" htmlFor="s-source" width="w-44">
          <Select id="s-source" value={f.source_authority} onChange={(e) => set({ source_authority: e.target.value })}>
            <option value="">Any</option>
            {facets.data?.source_authorities.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="Type" htmlFor="s-type" width="w-36">
          <Select id="s-type" value={f.sanction_type} onChange={(e) => set({ sanction_type: e.target.value })}>
            <option value="">Any</option>
            {facets.data?.sanction_types.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="State" htmlFor="s-state" width="w-24">
          <Select id="s-state" value={f.state} onChange={(e) => set({ state: e.target.value })}>
            <option value="">Any</option>
            {facets.data?.states.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="Record type" htmlFor="s-rtype" width="w-32">
          <Select id="s-rtype" value={f.record_type} onChange={(e) => set({ record_type: e.target.value })}>
            <option value="">Any</option>
            <option value="individual">Individual</option>
            <option value="organization">Organization</option>
          </Select>
        </FilterField>
        <FilterField label="Versions" htmlFor="s-history" width="w-36">
          <Select id="s-history" value={f.history} onChange={(e) => set({ history: e.target.value })}>
            <option value="">Current only</option>
            <option value="true">Include replaced</option>
          </Select>
        </FilterField>
      </FilterBar>
      <DataTable
        id="sanctions"
        caption="Sanction records"
        columns={RECORD_COLUMNS}
        data={records.data?.items}
        loading={records.isFetching}
        error={records.error}
        onRetry={() => void records.refetch()}
        getRowId={(row) => row.id}
        paging={{
          offset: Number(f.offset),
          limit: Number(f.limit),
          total: records.data?.total ?? 0,
          onChange: (offset, limit) => set({ offset: String(offset), limit: String(limit) }, { resetPage: false }),
        }}
        empty={<EmptyState title="No sanction records" body="Upload an exclusion list to begin." action={<Link className="text-accent hover:underline" to="/sanctions/upload">Upload a list</Link>} />}
      />
    </>
  );
}

const FILE_LIMIT = 25;

function Files({ offset, onOffset }: { offset: number; onOffset: (offset: number) => void }) {
  const files = useSanctionFiles(offset, FILE_LIMIT);
  const run = useStartRun();
  const navigate = useNavigate();

  const reconcile = async (file: SanctionFile) => {
    try {
      const started = await run.mutateAsync({ file_id: file.id });
      toast.success("Reconciliation queued", {
        description: `Run ${started.id.slice(0, 8)} over ${file.filename}`,
        action: { label: "Watch it", onClick: () => navigate(`/sanctions/upload?run=${started.id}&file=${file.id}`) },
      });
    } catch (error) {
      toast.error("Could not start the run", { description: error instanceof Error ? error.message : String(error) });
    }
  };

  const fcol = columnsFor<SanctionFile>();
  const columns = fcol.columns([
    fcol.accessor("filename", { header: "File", enableHiding: false, enableSorting: false, cell: (c) => <span className="font-medium text-ink">{c.getValue()}</span> }),
    fcol.accessor("status", { header: "Status", enableSorting: false, cell: (c) => <StatusBadge status={c.getValue()} /> }),
    fcol.accessor("source_authority", { header: "Source", enableSorting: false, cell: (c) => c.getValue() ?? "—" }),
    fcol.accessor("mapping_name", {
      header: "Read through mapping",
      enableSorting: false,
      cell: (c) => (c.getValue() ? <span className="text-ink">{c.getValue()}</span> : <span className="text-muted">—</span>),
    }),
    fcol.accessor("row_count", { header: "Rows", enableSorting: false, cell: (c) => <span className="tabular">{formatInt(c.getValue())}</span> }),
    fcol.accessor("summary", {
      header: "Ingested",
      enableSorting: false,
      cell: (c) => {
        const s = c.getValue() as { new?: number; replaced?: number; unchanged?: number; rejected?: number } | null | undefined;
        if (!s) return <span className="text-muted">—</span>;
        return (
          <span className="tabular text-xs text-ink-2">
            {formatInt(s.new)} new · {formatInt(s.replaced)} replaced · {formatInt(s.unchanged)} unchanged · {formatInt(s.rejected)} rejected
          </span>
        );
      },
    }),
    fcol.accessor("uploaded_at", { header: "Uploaded", enableSorting: false, cell: (c) => <span className="tabular text-xs">{formatDateTime(c.getValue())}</span> }),
    fcol.accessor("sha256", { header: "SHA-256", enableSorting: false, cell: (c) => <span className="font-mono text-xs text-muted" title={c.getValue()}>{c.getValue().slice(0, 12)}</span> }),
    fcol.display({
      id: "actions",
      header: "",
      enableHiding: false,
      cell: ({ row }) =>
        row.original.status === "COMMITTED" ? (
          <Button size="sm" onClick={() => void reconcile(row.original)} loading={run.isPending && run.variables?.file_id === row.original.id}>
            <Play /> Reconcile
          </Button>
        ) : row.original.status === "INSPECTED" ? (
          <span className="text-xs text-muted">Awaiting commit</span>
        ) : null,
    }),
  ]);

  return (
    <DataTable
      id="sanction-files"
      caption="Uploaded files"
      columns={columns}
      data={files.data?.items}
      loading={files.isFetching}
      error={files.error}
      onRetry={() => void files.refetch()}
      getRowId={(row) => row.id}
      paging={{ offset, limit: FILE_LIMIT, total: files.data?.total ?? 0, onChange: (o) => onOffset(o) }}
      empty={<EmptyState title="No files uploaded yet" action={<Link className="text-accent hover:underline" to="/sanctions/upload">Upload a list</Link>} />}
    />
  );
}
