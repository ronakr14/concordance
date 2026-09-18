import { useNavigate } from "react-router";

import { type AuditFilters, useAudit } from "@/api/queries";
import { AuditTimeline } from "@/components/AuditTimeline";
import { FilterBar, FilterField, SavedViews } from "@/components/FilterBar";
import { EmptyState, ErrorState, PageHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input, Select } from "@/components/ui/form";
import { Card, CardBody, Skeleton } from "@/components/ui/surface";
import { formatInt } from "@/lib/format";
import { opt, useSearchState } from "@/lib/useSearchState";

const DEFAULTS = { entity_type: "", entity_id: "", actor_user_id: "", action: "", date_from: "", date_to: "", offset: "0", limit: "50" };

/** Action families the filter offers. A value ending in `.` matches the whole family. */
const ACTIONS = [
  ["", "Any action"],
  ["match.", "Match — any"],
  ["match.viewed", "Match viewed"],
  ["match.ai_decided", "AI decision"],
  ["match.approved", "Match approved"],
  ["match.rejected", "Match rejected"],
  ["match.escalated", "Match escalated"],
  ["match.bulk_reviewed", "Bulk review"],
  ["case.", "Case — any"],
  ["case.opened", "Case opened"],
  ["case.closed", "Case closed"],
  ["case.expired", "Case expired"],
  ["case.conflict_flagged", "Case conflict flagged"],
  ["sanction_file.", "Upload — any"],
  ["run.", "Run — any"],
  ["column_mapping.", "Column mapping — any"],
  ["user.", "Sign-in and accounts"],
] as const;

export function AuditPage() {
  const navigate = useNavigate();
  const { values: f, set, reset, active, query } = useSearchState(DEFAULTS);
  const filters: AuditFilters = {
    entity_type: opt(f.entity_type),
    entity_id: opt(f.entity_id),
    actor_user_id: opt(f.actor_user_id),
    action: opt(f.action),
    date_from: opt(f.date_from),
    date_to: opt(f.date_to),
    offset: Number(f.offset),
    limit: Number(f.limit),
  };
  const audit = useAudit(filters);
  const offset = Number(f.offset);
  const limit = Number(f.limit);
  const total = audit.data?.total ?? 0;

  return (
    <>
      <PageHeader
        title="Audit log"
        description="Every mutation, sign-in and view, append-only. The database role the app runs as cannot update or delete a row here."
        actions={<SavedViews storageKey="audit" query={query} onApply={(q) => navigate(`/audit?${q}`)} />}
      />
      <FilterBar active={active} onReset={reset}>
        <FilterField label="Entity" htmlFor="a-entity" width="w-40">
          <Select id="a-entity" value={f.entity_type} onChange={(e) => set({ entity_type: e.target.value })}>
            <option value="">Any</option>
            <option value="match_result">Match result</option>
            <option value="case">Case</option>
            <option value="sanction_file">Sanction file</option>
            <option value="reconciliation_run">Run</option>
            <option value="column_mapping">Column mapping</option>
            <option value="user">User</option>
            <option value="review_batch">Bulk review</option>
          </Select>
        </FilterField>
        <FilterField label="Entity ID" htmlFor="a-entity-id" width="w-64">
          <Input id="a-entity-id" value={f.entity_id} onChange={(e) => set({ entity_id: e.target.value.trim() })} placeholder="UUID" />
        </FilterField>
        <FilterField label="Action" htmlFor="a-action" width="w-48">
          <Select id="a-action" value={f.action} onChange={(e) => set({ action: e.target.value })}>
            {ACTIONS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </Select>
        </FilterField>
        <FilterField label="Actor (user ID)" htmlFor="a-actor" width="w-64">
          <Input id="a-actor" value={f.actor_user_id} onChange={(e) => set({ actor_user_id: e.target.value.trim() })} placeholder="UUID" />
        </FilterField>
        <FilterField label="Between" htmlFor="a-from" width="w-64">
          <div className="flex items-center gap-1">
            <Input id="a-from" type="date" value={f.date_from} onChange={(e) => set({ date_from: e.target.value })} aria-label="From date" />
            <Input type="date" value={f.date_to} onChange={(e) => set({ date_to: e.target.value })} aria-label="To date" />
          </div>
        </FilterField>
      </FilterBar>
      <Card>
        <CardBody className="pt-4">
          {audit.error ? (
            <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />
          ) : !audit.data ? (
            <div className="space-y-3">
              {Array.from({ length: 6 }, (_, i) => (
                <Skeleton key={i} className="h-10" />
              ))}
            </div>
          ) : audit.data.items.length === 0 ? (
            <EmptyState title="No events match" body="Widen the date range or clear a filter." />
          ) : (
            <div className={audit.isFetching ? "opacity-60" : undefined}>
              <AuditTimeline rows={audit.data.items} showEntity />
            </div>
          )}
        </CardBody>
        <div className="flex items-center justify-between border-t px-4 py-2 text-xs text-muted">
          <span className="tabular">
            {total === 0 ? 0 : offset + 1}–{offset + (audit.data?.items.length ?? 0)} of {formatInt(total)}
          </span>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" disabled={offset === 0} onClick={() => set({ offset: String(Math.max(0, offset - limit)) }, { resetPage: false })}>
              Newer
            </Button>
            <Button size="sm" variant="ghost" disabled={offset + limit >= total} onClick={() => set({ offset: String(offset + limit) }, { resetPage: false })}>
              Older
            </Button>
          </div>
        </div>
      </Card>
    </>
  );
}
