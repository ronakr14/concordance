import type { RowSelectionState } from "@tanstack/react-table";
import { CircleArrowUp, CircleX, TriangleAlert } from "lucide-react";
import { type FormEvent, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { toast } from "sonner";

import { type MatchFilters, useBulkReview, useFacets, useMatches } from "@/api/queries";
import type { MatchListItem } from "@/api/types";
import { useIsAdmin } from "@/auth/AuthProvider";
import { columnsFor, DataTable } from "@/components/DataTable";
import { FilterBar, FilterField, SavedViews } from "@/components/FilterBar";
import { PageHeader } from "@/components/states";
import { ConfidenceBadge, RouteBadge, StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Input, Select, Textarea } from "@/components/ui/form";
import { DialogContent, DialogRoot, Tip } from "@/components/ui/overlay";
import { Kbd } from "@/components/ui/surface";
import { formatDateTime } from "@/lib/format";
import { RECORD_TYPE } from "@/lib/status";
import { opt, optNum, useSearchState } from "@/lib/useSearchState";
import { rememberQueue } from "@/lib/queueOrder";

const DEFAULTS = {
  decision: "",
  review_status: "PENDING",
  min_confidence: "",
  max_confidence: "",
  state: "",
  sanction_type: "",
  record_type: "",
  conflict: "",
  date_from: "",
  date_to: "",
  run_id: "",
  sort: "confidence",
  order: "desc",
  offset: "0",
  limit: "50",
};

const col = columnsFor<MatchListItem>();
const COLUMNS = col.columns([
  col.accessor("subject_name", {
    header: "Sanctioned party",
    enableSorting: false,
    enableHiding: false,
    cell: ({ row }) => {
      const type = RECORD_TYPE[row.original.is_organization ? "organization" : "individual"];
      return (
        <div className="flex min-w-0 items-center gap-2">
          <type.icon className="size-3.5 shrink-0 text-muted" aria-label={type.label} />
          <div className="min-w-0">
            <div className="truncate font-medium text-ink">{row.original.subject_name || "—"}</div>
            <div className="truncate text-xs text-muted">
              {row.original.record_id} · {row.original.source_authority ?? "unknown source"}
            </div>
          </div>
        </div>
      );
    },
  }),
  col.accessor("decision", { header: "Decision", enableSorting: false, cell: (c) => <StatusBadge status={c.getValue()} /> }),
  col.accessor("calibrated_confidence", {
    id: "confidence",
    header: "Confidence",
    cell: (c) => <ConfidenceBadge value={c.getValue()} />,
  }),
  col.accessor("review_status", { header: "Review", enableSorting: false, cell: (c) => <StatusBadge status={c.getValue()} /> }),
  col.accessor("route", { header: "Route", enableSorting: false, cell: (c) => <RouteBadge route={c.getValue()} /> }),
  col.accessor("chosen_provider_id", {
    header: "Proposed provider",
    enableSorting: false,
    cell: (c) => <span className="font-mono text-xs">{c.getValue() ?? "—"}</span>,
  }),
  col.accessor("state", { header: "State", enableSorting: false, cell: (c) => c.getValue() ?? "—" }),
  col.accessor("sanction_type", { header: "Sanction type", enableSorting: false, cell: (c) => c.getValue() ?? "—" }),
  col.accessor("created_at", {
    id: "date",
    header: "Decided",
    cell: (c) => <span className="tabular text-xs text-ink-2">{formatDateTime(c.getValue())}</span>,
  }),
]);

export function QueuePage() {
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const { values: f, set, reset, active, query } = useSearchState(DEFAULTS);
  const facets = useFacets();
  const [selected, setSelected] = useState<RowSelectionState>({});
  const [cursor, setCursor] = useState(0);

  const filters: MatchFilters = {
    decision: opt(f.decision) as MatchFilters["decision"],
    review_status: opt(f.review_status) as MatchFilters["review_status"],
    min_confidence: optNum(f.min_confidence),
    max_confidence: optNum(f.max_confidence),
    state: opt(f.state),
    sanction_type: opt(f.sanction_type),
    record_type: opt(f.record_type) as MatchFilters["record_type"],
    conflict: f.conflict === "" ? undefined : f.conflict === "true",
    date_from: opt(f.date_from),
    date_to: opt(f.date_to),
    run_id: opt(f.run_id),
    sort: f.sort as MatchFilters["sort"],
    order: f.order as MatchFilters["order"],
    offset: Number(f.offset),
    limit: Number(f.limit),
  };
  const matches = useMatches(filters);
  const items = useMemo(() => matches.data?.items ?? [], [matches.data]);

  // A new page or new filters: selection and cursor start over.
  useEffect(() => {
    setSelected({});
    setCursor(0);
  }, [query]);

  useEffect(() => {
    if (items.length) rememberQueue({ ids: items.map((m) => m.id), query });
  }, [items, query]);

  const open = (item: MatchListItem) => {
    rememberQueue({ ids: items.map((m) => m.id), query });
    navigate(`/queue/${item.id}`);
  };

  // j / k move the cursor, Enter opens: an analyst works in volume.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement;
      if (target.closest("input, textarea, select, [role=dialog], [role=menu]")) return;
      if (event.key === "j" || event.key === "ArrowDown") {
        event.preventDefault();
        setCursor((c) => Math.min(c + 1, Math.max(items.length - 1, 0)));
      } else if (event.key === "k" || event.key === "ArrowUp") {
        event.preventDefault();
        setCursor((c) => Math.max(c - 1, 0));
      } else if (event.key === "Enter" && items[cursor]) {
        open(items[cursor]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  useEffect(() => {
    const id = items[cursor]?.id;
    if (id) document.querySelector(`[data-row-id="${id}"]`)?.scrollIntoView({ block: "nearest" });
  }, [cursor, items]);

  const selectedIds = Object.keys(selected).filter((id) => selected[id]);

  return (
    <>
      <PageHeader
        title="Review queue"
        description="Engine decisions awaiting a human verdict. Filters live in the URL, so a view can be bookmarked or shared."
        actions={
          <span className="hidden items-center gap-1 text-xs text-muted lg:inline-flex">
            <Kbd>j</Kbd>
            <Kbd>k</Kbd> move · <Kbd>Enter</Kbd> open
          </span>
        }
      />

      <FilterBar active={active} onReset={reset}>
        <FilterField label="Review" htmlFor="f-review" width="w-32">
          <Select id="f-review" value={f.review_status} onChange={(e) => set({ review_status: e.target.value })}>
            <option value="">Any</option>
            <option value="PENDING">Pending</option>
            <option value="ESCALATED">Escalated</option>
            <option value="APPROVED">Approved</option>
            <option value="REJECTED">Rejected</option>
          </Select>
        </FilterField>
        <FilterField label="Decision" htmlFor="f-decision" width="w-32">
          <Select id="f-decision" value={f.decision} onChange={(e) => set({ decision: e.target.value })}>
            <option value="">Any</option>
            <option value="MATCH">Match</option>
            <option value="AMBIGUOUS">Ambiguous</option>
            <option value="NO_MATCH">Unmatched</option>
          </Select>
        </FilterField>
        <FilterField label="Confidence (%)" htmlFor="f-cmin" width="w-36">
          <div className="flex items-center gap-1">
            <Input
              id="f-cmin"
              inputMode="decimal"
              placeholder="min"
              aria-label="Minimum confidence percent"
              value={f.min_confidence === "" ? "" : String(Math.round(Number(f.min_confidence) * 100))}
              onChange={(e) => set({ min_confidence: pctParam(e.target.value) })}
            />
            <Input
              inputMode="decimal"
              placeholder="max"
              aria-label="Maximum confidence percent"
              value={f.max_confidence === "" ? "" : String(Math.round(Number(f.max_confidence) * 100))}
              onChange={(e) => set({ max_confidence: pctParam(e.target.value) })}
            />
          </div>
        </FilterField>
        <FilterField label="State" htmlFor="f-state" width="w-24">
          <Select id="f-state" value={f.state} onChange={(e) => set({ state: e.target.value })}>
            <option value="">Any</option>
            {facets.data?.states.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="Sanction type" htmlFor="f-type" width="w-36">
          <Select id="f-type" value={f.sanction_type} onChange={(e) => set({ sanction_type: e.target.value })}>
            <option value="">Any</option>
            {facets.data?.sanction_types.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="Record type" htmlFor="f-rtype" width="w-32">
          <Select id="f-rtype" value={f.record_type} onChange={(e) => set({ record_type: e.target.value })}>
            <option value="">Any</option>
            <option value="individual">Individual</option>
            <option value="organization">Organization</option>
          </Select>
        </FilterField>
        <FilterField label="Conflicts" htmlFor="f-conflict" width="w-32">
          <Select id="f-conflict" value={f.conflict} onChange={(e) => set({ conflict: e.target.value })}>
            <option value="">Any</option>
            <option value="true">Conflicts only</option>
            <option value="false">No conflict</option>
          </Select>
        </FilterField>
        <FilterField label="Decided between" htmlFor="f-from" width="w-64">
          <div className="flex items-center gap-1">
            <Input id="f-from" type="date" value={f.date_from} onChange={(e) => set({ date_from: e.target.value })} aria-label="From date" />
            <Input type="date" value={f.date_to} onChange={(e) => set({ date_to: e.target.value })} aria-label="To date" />
          </div>
        </FilterField>
      </FilterBar>

      <DataTable
        id="queue"
        caption="Review queue"
        columns={COLUMNS}
        data={matches.data?.items}
        loading={matches.isFetching}
        error={matches.error}
        onRetry={() => void matches.refetch()}
        getRowId={(row) => row.id}
        onRowClick={open}
        activeRowId={items[cursor]?.id ?? null}
        paging={{
          offset: Number(f.offset),
          limit: Number(f.limit),
          total: matches.data?.total ?? 0,
          onChange: (offset, limit) => set({ offset: String(offset), limit: String(limit) }, { resetPage: false }),
        }}
        sorting={{
          sort: f.sort,
          order: f.order as "asc" | "desc",
          onChange: (sort, order) => set({ sort, order }),
        }}
        selection={{
          selected,
          onChange: setSelected,
          // Only an undecided result can be batch-decided; an analyst cannot
          // reject what was escalated to an admin.
          canSelect: (row) => row.review_status === "PENDING" || (isAdmin && row.review_status === "ESCALATED"),
        }}
        toolbar={
          <>
            {selectedIds.length > 0 ? (
              <BulkActions ids={selectedIds} onDone={() => setSelected({})} />
            ) : (
              <span className="text-xs text-muted">Select rows to reject or escalate them together.</span>
            )}
            <div className="ml-auto">
              <SavedViews storageKey="queue" query={query} onApply={(q) => navigate(`/queue?${q}`)} />
            </div>
          </>
        }
      />
    </>
  );
}

/** "85" -> "0.85". Empty stays empty; nonsense is ignored. */
function pctParam(raw: string): string {
  if (raw.trim() === "") return "";
  const n = Number(raw);
  if (!Number.isFinite(n)) return "";
  return String(Math.min(100, Math.max(0, n)) / 100);
}

function BulkActions({ ids, onDone }: { ids: string[]; onDone: () => void }) {
  const [action, setAction] = useState<"reject" | "escalate" | null>(null);
  const [comment, setComment] = useState("");
  const bulk = useBulkReview();

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!action || !comment.trim()) return;
    try {
      const result = await bulk.mutateAsync({ ids, action, comment: comment.trim() });
      const verb = action === "reject" ? "Rejected" : "Escalated";
      if (result.failed === 0) toast.success(`${verb} ${result.succeeded} result${result.succeeded === 1 ? "" : "s"}`);
      else
        toast.warning(`${verb} ${result.succeeded}; ${result.failed} refused`, {
          description: result.results
            .filter((r) => !r.ok)
            .slice(0, 3)
            .map((r) => r.error?.message)
            .join(" · "),
        });
      setAction(null);
      setComment("");
      onDone();
    } catch (error) {
      toast.error("Bulk review failed", { description: error instanceof Error ? error.message : String(error) });
    }
  };

  return (
    <>
      <span className="text-xs font-medium text-ink">{ids.length} selected</span>
      <Button size="sm" onClick={() => setAction("escalate")}>
        <CircleArrowUp /> Escalate
      </Button>
      <Button size="sm" onClick={() => setAction("reject")}>
        <CircleX /> Reject
      </Button>
      <Tip content="Approval opens a compliance case, so it is always one record at a time.">
        <span className="inline-flex items-center gap-1 text-xs text-muted">
          <TriangleAlert className="size-3.5" /> No bulk approve
        </span>
      </Tip>
      <DialogRoot open={action !== null} onOpenChange={(open) => !open && setAction(null)}>
        <DialogContent
          title={action === "reject" ? `Reject ${ids.length} results` : `Escalate ${ids.length} results`}
          description="One comment applies to every selected result. Each is still decided and audited on its own."
        >
          <form onSubmit={submit} className="space-y-3">
            <Textarea autoFocus aria-label="Comment" required value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Say why" />
            <div className="flex justify-end gap-2">
              <Button onClick={() => setAction(null)}>Cancel</Button>
              <Button type="submit" variant={action === "reject" ? "danger" : "primary"} loading={bulk.isPending} disabled={!comment.trim()}>
                {action === "reject" ? "Reject" : "Escalate"} {ids.length}
              </Button>
            </div>
          </form>
        </DialogContent>
      </DialogRoot>
    </>
  );
}
