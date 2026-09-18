import { ArrowLeft } from "lucide-react";
import { Link, useNavigate, useParams } from "react-router";

import { useProvider } from "@/api/queries";
import type { ProviderMatch } from "@/api/types";
import { columnsFor, DataTable } from "@/components/DataTable";
import { EmptyState, ErrorState, Field } from "@/components/states";
import { ConfidenceBadge, ConflictTag, StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Card, CardHeader, Skeleton } from "@/components/ui/surface";
import { formatDate, formatDateTime } from "@/lib/format";
import { RECORD_TYPE } from "@/lib/status";

import { providerName } from "./investigation/fields";

const col = columnsFor<ProviderMatch>();
const MATCH_COLUMNS = col.columns([
  col.accessor("subject_name", {
    header: "Sanction record",
    enableHiding: false,
    cell: ({ row }) => (
      <div>
        <div className="font-medium text-ink">{row.original.subject_name || "—"}</div>
        <div className="text-xs text-muted">
          {row.original.record_id} · {row.original.source_authority ?? "unknown source"}
        </div>
      </div>
    ),
  }),
  col.accessor("candidate_rank", { header: "Rank", cell: (c) => <span className="tabular">#{c.getValue()}</span> }),
  col.accessor("candidate_posterior", { header: "Posterior", cell: (c) => <ConfidenceBadge value={c.getValue()} /> }),
  col.accessor("decision", { header: "Decision", cell: (c) => <StatusBadge status={c.getValue()} /> }),
  col.accessor("review_status", { header: "Review", cell: (c) => <StatusBadge status={c.getValue()} /> }),
  col.accessor("superseded_by", {
    header: "Current",
    cell: (c) => (c.getValue() ? <span className="text-xs text-muted">Superseded</span> : <span className="text-xs text-ink">Current</span>),
  }),
  col.accessor("created_at", { header: "Decided", cell: (c) => <span className="tabular text-xs">{formatDateTime(c.getValue())}</span> }),
]);

export function ProviderPage() {
  const { providerId = "" } = useParams();
  const navigate = useNavigate();
  const provider = useProvider(providerId);

  if (provider.error) return <ErrorState error={provider.error} onRetry={() => void provider.refetch()} />;
  const p = provider.data;
  const type = p ? RECORD_TYPE[p.is_organization ? "organization" : "individual"] : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate(-1)}>
          <ArrowLeft /> Back
        </Button>
        {p && type ? (
          <>
            <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
              <type.icon className="size-5 text-muted" aria-label={type.label} /> {providerName(p)}
            </h1>
            <StatusBadge status={p.compliance_status} />
            <span className="font-mono text-xs text-muted">{p.provider_id}</span>
          </>
        ) : (
          <Skeleton className="h-7 w-72" />
        )}
      </div>

      <div className="grid gap-4 lg:grid-cols-[22rem_1fr]">
        <Card>
          <CardHeader title="Master record" />
          {p ? (
            <dl className="px-4 pb-3">
              <Field label="NPI" mono>{p.npi}</Field>
              {p.is_organization ? (
                <>
                  <Field label="Legal name">{p.organization_name}</Field>
                  <Field label="DBA">{p.dba_name}</Field>
                  <Field label="EIN" mono>{p.ein}</Field>
                </>
              ) : (
                <>
                  <Field label="Name">{[p.first_name, p.middle_name, p.last_name, p.suffix].filter(Boolean).join(" ")}</Field>
                  <Field label="Date of birth">{p.dob ? formatDate(p.dob) : null}</Field>
                  <Field label="Specialty">{p.specialty}</Field>
                  <Field label="Organization">{p.organization_name}</Field>
                  <Field label="License" mono>{[p.license_number, p.license_state].filter(Boolean).join(" · ")}</Field>
                </>
              )}
              <Field label="Address">{[p.address_line1, p.address_line2].filter(Boolean).join(", ")}</Field>
              <Field label="City / state">{[p.city, p.state, p.zip].filter(Boolean).join(", ")}</Field>
              <Field label="Master status">{p.status}</Field>
            </dl>
          ) : (
            <div className="space-y-2 px-4 pb-4">
              {Array.from({ length: 8 }, (_, i) => (
                <Skeleton key={i} className="h-5" />
              ))}
            </div>
          )}
        </Card>

        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader title="Compliance history" description="Every case opened against this provider." />
            {p && p.cases.length === 0 ? (
              <EmptyState title="No cases" body="No approved match has ever named this provider." />
            ) : (
              <ul className="divide-y px-4 pb-2">
                {(p?.cases ?? []).map((c) => (
                  <li key={c.id} className="flex flex-wrap items-center gap-3 py-2 text-sm">
                    <Link to={`/cases/${c.id}`} className="font-mono text-accent hover:underline">
                      {c.case_number}
                    </Link>
                    <StatusBadge status={c.status} />
                    <span className="text-xs text-muted">
                      {formatDate(c.start_date)} – {formatDate(c.end_date)} · {c.duration_months} months
                    </span>
                    {c.conflict_flag ? <ConflictTag /> : null}
                  </li>
                ))}
                {!p ? <Skeleton className="my-2 h-6" /> : null}
              </ul>
            )}
          </Card>
          <div>
            <h2 className="mb-2 text-sm font-semibold">Reconciliation history</h2>
            <DataTable
              id="provider-matches"
              caption="Results that ranked this provider"
              columns={MATCH_COLUMNS}
              data={p?.matches}
              loading={provider.isLoading}
              getRowId={(row) => row.id}
              onRowClick={(row) => navigate(`/queue/${row.id}`)}
              empty={<EmptyState title="Never a candidate" body="No sanction record has been compared against this provider." />}
              dense
            />
          </div>
        </div>
      </div>
    </div>
  );
}
