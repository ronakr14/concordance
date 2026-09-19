import { ArrowLeft, GitCompare, Lock, ScrollText } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { toast } from "sonner";

import { useCase, useCaseAudit, useCloseCase } from "@/api/queries";
import type { AuditRow, CaseDetail } from "@/api/types";
import { AdminOnly } from "@/auth/guards";
import { AuditTimeline } from "@/components/AuditTimeline";
import { EmptyState, ErrorState, Field } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { FieldError, Label, Textarea } from "@/components/ui/form";
import { DialogContent, DialogRoot } from "@/components/ui/overlay";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui/surface";
import { formatDate, formatDateTime } from "@/lib/format";
import { casePhaseKey, statusSpec, TONE_CLASS } from "@/lib/status";
import { cn } from "@/lib/utils";

export function CaseDetailPage() {
  const { caseId = "" } = useParams();
  const navigate = useNavigate();
  const detail = useCase(caseId);
  const audit = useCaseAudit(caseId);

  if (detail.error) return <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />;
  const c = detail.data;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => navigate("/cases")}>
            <ArrowLeft /> Cases
          </Button>
          {c ? (
            <>
              <h1 className="font-mono text-xl font-semibold tracking-tight">{c.case_number}</h1>
              <StatusBadge status={casePhaseKey(c.phase)} />
            </>
          ) : (
            <Skeleton className="h-7 w-56" />
          )}
        </div>
        {c ? (
          <div className="flex items-center gap-2">
            <AdminOnly>
              <Button variant="ghost" size="sm" onClick={() => navigate(`/audit?entity_type=case&entity_id=${c.id}`)}>
                <ScrollText /> In the audit log
              </Button>
            </AdminOnly>
            {c.status === "ACTIVE" ? (
              <AdminOnly>
                <CloseCase caseId={c.id} />
              </AdminOnly>
            ) : null}
          </div>
        ) : null}
      </div>

      {c?.conflict_flag ? (
        <div className="flex items-center gap-3 rounded-lg bg-serious/12 px-4 py-3 ring-1 ring-serious/40" role="alert">
          <GitCompare className="size-5 shrink-0 text-serious-text" aria-hidden />
          <p className="text-sm text-ink">
            A newer reconciliation run disagrees with the decision this case was opened on. The case was left as it is.{" "}
            {c.conflict_match_result_id ? (
              <Link to={`/queue/${c.conflict_match_result_id}`} className="font-medium text-accent hover:underline">
                Review the contradicting result
              </Link>
            ) : null}
          </p>
        </div>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-[1fr_26rem]">
        <div className="min-w-0 space-y-4">
          <Card>
            <CardHeader title="Status timeline" description="The case window, and every transition in it." />
            <CardBody>{c && audit.data ? <StatusTimeline c={c} events={audit.data.items} /> : <Skeleton className="h-16" />}</CardBody>
          </Card>
          <Card>
            <CardHeader title="Audit history" description="Every recorded event on this case, newest first." />
            <CardBody>
              {audit.error ? (
                <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />
              ) : !audit.data ? (
                <Skeleton className="h-32" />
              ) : audit.data.items.length === 0 ? (
                <EmptyState title="No events recorded" />
              ) : (
                <AuditTimeline rows={audit.data.items} />
              )}
            </CardBody>
          </Card>
        </div>
        <div className="space-y-4">
          <Card>
            <CardHeader title="Case" />
            {c ? (
              <dl className="px-4 pb-3">
                <Field label="Window">
                  {formatDate(c.start_date)} – {formatDate(c.end_date)}
                </Field>
                <Field label="Duration">{c.duration_months} months</Field>
                <Field label="Opened">{formatDateTime(c.created_at)}</Field>
                <Field label="Approved by">{c.created_by_email ?? (c.created_by ? c.created_by.slice(0, 8) : "System")}</Field>
                {c.match ? (
                  <Field label="Approving match">
                    <Link className="text-accent hover:underline" to={`/queue/${c.match.id}`}>
                      Open the investigation
                    </Link>
                    {c.match.reviewer_comment ? <p className="mt-0.5 text-xs text-ink-2">“{c.match.reviewer_comment}”</p> : null}
                  </Field>
                ) : null}
                {c.close_reason ? (
                  <Field label="Closed">
                    {c.close_reason}
                    <span className="block text-xs text-muted">by {c.closed_by_email ?? "unknown"}</span>
                  </Field>
                ) : null}
              </dl>
            ) : (
              <Skeleton className="mx-4 mb-4 h-40" />
            )}
          </Card>
          <Card>
            <CardHeader title="Provider" />
            {c?.provider ? (
              <dl className="px-4 pb-3">
                <Field label="Name">
                  <Link className="text-accent hover:underline" to={`/providers/${encodeURIComponent(c.provider.provider_id)}`}>
                    {c.provider_name ?? c.provider.provider_id}
                  </Link>
                </Field>
                <Field label="Provider ID" mono>{c.provider.provider_id}</Field>
                <Field label="NPI" mono>{c.provider.npi}</Field>
                <Field label="Location">{[c.provider.city, c.provider.state].filter(Boolean).join(", ")}</Field>
              </dl>
            ) : (
              <Skeleton className="mx-4 mb-4 h-24" />
            )}
          </Card>
          <Card>
            <CardHeader title="Sanction" />
            {c?.sanction_record ? (
              <dl className="px-4 pb-3">
                <Field label="Subject">{c.subject_name}</Field>
                <Field label="Record" mono>{c.sanction_record.record_id}</Field>
                <Field label="Source">{c.sanction_record.source_authority}</Field>
                <Field label="Type">{c.sanction_record.sanction_type}</Field>
                <Field label="Excluded">{formatDate(c.sanction_record.exclusion_date)}</Field>
              </dl>
            ) : (
              <Skeleton className="mx-4 mb-4 h-24" />
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

/**
 * The case window as a track, today marked on it, and each audited transition
 * as a stop beneath it - so "expired by the system on the 31st" reads at a glance.
 */
function StatusTimeline({ c, events }: { c: CaseDetail; events: AuditRow[] }) {
  const start = Date.parse(c.start_date);
  const end = Date.parse(c.end_date);
  const now = Date.now();
  const span = Math.max(end - start, 1);
  const at = (t: number) => `${Math.min(100, Math.max(0, ((t - start) / span) * 100))}%`;
  const transitions = [...events].filter((e) => e.action.startsWith("case.")).reverse();
  const tone = TONE_CLASS[statusSpec(casePhaseKey(c.phase)).tone];

  return (
    <div className="space-y-3">
      <div className="flex justify-between text-xs text-muted">
        <span>Start {formatDate(c.start_date)}</span>
        <span>End {formatDate(c.end_date)}</span>
      </div>
      <div className="relative h-2 rounded-full bg-surface-3">
        <div className={cn("absolute inset-y-0 left-0 rounded-full opacity-60", tone.bar)} style={{ width: c.status === "ACTIVE" ? at(now) : "100%" }} />
        {now >= start && now <= end ? (
          <div className="absolute -top-1 h-4 w-0.5 bg-ink" style={{ left: at(now) }} title="Today" aria-label="Today" />
        ) : null}
      </div>
      <ol className="flex flex-wrap gap-2">
        {transitions.map((e) => {
          const status = e.action === "case.opened" ? "ACTIVE" : e.action === "case.expired" ? "EXPIRED" : e.action === "case.closed" ? "CLOSED" : null;
          return (
            <li key={e.id} className="flex items-center gap-2 rounded-md bg-surface-2 px-2 py-1 text-xs">
              {status ? <StatusBadge status={status} /> : <span className="font-medium">{e.action.replace("case.", "")}</span>}
              <span className="tabular text-muted">{formatDateTime(e.created_at)}</span>
              <span className="text-ink-2">{e.actor_user_id ? (e.actor_email ?? "user") : "System"}</span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function CloseCase({ caseId }: { caseId: string }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const close = useCloseCase(caseId);
  const valid = reason.trim().length >= 3;

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    try {
      await close.mutateAsync(reason.trim());
      setOpen(false);
      toast.success("Case closed");
    } catch (error) {
      toast.error("Could not close the case", { description: error instanceof Error ? error.message : String(error) });
    }
  };

  return (
    <DialogRoot open={open} onOpenChange={setOpen}>
      <Button variant="danger" size="sm" onClick={() => setOpen(true)}>
        <Lock /> Close case…
      </Button>
      <DialogContent title="Close this case early" description="Ends the case before its window does. The reason is kept in the audit log.">
        <form onSubmit={submit} className="space-y-3">
          <div className="space-y-1">
            <Label htmlFor="close-reason">Reason</Label>
            <Textarea id="close-reason" autoFocus value={reason} onChange={(e) => setReason(e.target.value)} aria-invalid={reason.length > 0 && !valid} />
            <FieldError>{reason.length > 0 && !valid ? "At least three characters." : null}</FieldError>
          </div>
          <div className="flex justify-end gap-2">
            <Button onClick={() => setOpen(false)}>Cancel</Button>
            <Button type="submit" variant="danger" loading={close.isPending} disabled={!valid}>
              Close case
            </Button>
          </div>
        </form>
      </DialogContent>
    </DialogRoot>
  );
}
