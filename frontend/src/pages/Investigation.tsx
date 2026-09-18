import { ArrowLeft, ChevronLeft, ChevronRight, ScrollText } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";

import { AdminOnly } from "@/auth/guards";

import { useMatch } from "@/api/queries";
import type { MatchDetail } from "@/api/types";
import { useIsAdmin } from "@/auth/AuthProvider";
import { ErrorState, Field } from "@/components/states";
import { RouteBadge, StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/overlay";
import { Card, CardHeader, Kbd, Skeleton } from "@/components/ui/surface";
import { formatDate } from "@/lib/format";
import { neighbours } from "@/lib/queueOrder";
import { RECORD_TYPE } from "@/lib/status";

import { type ActionKind, ActionsPanel, allowedActions } from "./investigation/Actions";
import { EvidenceTable } from "./investigation/Evidence";
import { fieldsFor } from "./investigation/fields";
import { CandidateList, ConfidencePanel, ExplanationPanel, HistoryBanners, RecommendationBanner, splitEvidence } from "./investigation/Panels";

export function InvestigationPage() {
  const { matchId = "" } = useParams();
  const match = useMatch(matchId);

  if (match.error) return <ErrorState error={match.error} onRetry={() => void match.refetch()} />;
  if (!match.data) return <InvestigationSkeleton />;
  // Keyed by id: moving to the next item resets the selected candidate and dialogs.
  return <Investigation key={match.data.id} detail={match.data} />;
}

function Investigation({ detail }: { detail: MatchDetail }) {
  const navigate = useNavigate();
  const isAdmin = useIsAdmin();
  const nav = neighbours(detail.id);
  const [selected, setSelected] = useState<string | null>(
    detail.approved_provider_id ?? detail.chosen_provider_id ?? detail.candidates[0]?.provider_id ?? null,
  );
  const [dialog, setDialog] = useState<ActionKind | null>(null);
  const provider = detail.candidates.find((c) => c.provider_id === selected)?.provider ?? null;
  const cited = useMemo(() => new Set(detail.adjudication?.evidence_cited ?? []), [detail.adjudication]);
  const citedFields = useMemo(() => new Set([...cited].map((k) => splitEvidence(k)[1])), [cited]);
  const type = RECORD_TYPE[detail.sanction_record.is_organization ? "organization" : "individual"];

  // Keyboard: j/k (or ←/→) step through the queue; a/r/e open the verdicts.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (dialog || event.metaKey || event.ctrlKey || event.altKey) return;
      if ((event.target as HTMLElement).closest("input, textarea, select, [role=dialog], [role=menu]")) return;
      const allowed = allowedActions(detail, isAdmin);
      if ((event.key === "j" || event.key === "ArrowRight") && nav.next) navigate(`/queue/${nav.next}`);
      else if ((event.key === "k" || event.key === "ArrowLeft") && nav.prev) navigate(`/queue/${nav.prev}`);
      else if (event.key === "a" && allowed.has("approve")) setDialog("approve");
      else if (event.key === "r" && allowed.has("reject")) setDialog("reject");
      else if (event.key === "e" && allowed.has("escalate")) setDialog("escalate");
      else if (event.key === "Escape") navigate(`/queue${nav.query ? `?${nav.query}` : ""}`);
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [detail, isAdmin, nav.next, nav.prev, nav.query, navigate, dialog]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <Button variant="ghost" size="sm" onClick={() => navigate(`/queue${nav.query ? `?${nav.query}` : ""}`)}>
            <ArrowLeft /> Queue
          </Button>
          <div className="min-w-0">
            <h1 className="flex items-center gap-2 truncate text-xl font-semibold tracking-tight">
              <type.icon className="size-5 shrink-0 text-muted" aria-label={type.label} />
              {subject(detail)}
            </h1>
            <p className="text-xs text-muted">
              Record <span className="font-mono">{detail.sanction_record.record_id}</span> · {detail.sanction_record.source_authority ?? "unknown source"} · run{" "}
              <span className="font-mono">{detail.run_id.slice(0, 8)}</span>
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={detail.decision} />
          <StatusBadge status={detail.review_status} />
          <RouteBadge route={detail.route} />
          <AdminOnly>
            <Link to={`/audit?entity_type=match_result&entity_id=${detail.id}`} className="inline-flex items-center gap-1 text-xs text-accent hover:underline">
              <ScrollText className="size-3.5" /> Audit trail
            </Link>
          </AdminOnly>
          {nav.total > 0 && nav.position > 0 ? (
            <div className="ml-2 flex items-center gap-1 text-xs text-muted">
              <Tip content={<>Previous <Kbd>k</Kbd></>}>
                <Button size="icon" variant="ghost" aria-label="Previous in queue" disabled={!nav.prev} onClick={() => nav.prev && navigate(`/queue/${nav.prev}`)}>
                  <ChevronLeft />
                </Button>
              </Tip>
              <span className="tabular">
                {nav.position} of {nav.total}
              </span>
              <Tip content={<>Next <Kbd>j</Kbd></>}>
                <Button size="icon" variant="ghost" aria-label="Next in queue" disabled={!nav.next} onClick={() => nav.next && navigate(`/queue/${nav.next}`)}>
                  <ChevronRight />
                </Button>
              </Tip>
            </div>
          ) : null}
        </div>
      </div>

      <HistoryBanners detail={detail} />
      <RecommendationBanner detail={detail} />

      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="min-w-0 space-y-4">
          <RecordCard detail={detail} citedFields={citedFields} />
          <Card>
            <CardHeader
              title="Evidence, field by field"
              description="Each field's agreement level and the weight it contributed. Positive bits argue for a match, negative against."
            />
            <EvidenceTable detail={detail} provider={provider} cited={cited} />
          </Card>
          <ExplanationPanel detail={detail} />
        </div>
        <div className="space-y-4">
          <ActionsPanel detail={detail} selectedProvider={selected} open={dialog} onOpenChange={setDialog} />
          <ConfidencePanel detail={detail} />
          <CandidateList detail={detail} selected={selected} onSelect={setSelected} />
        </div>
      </div>
    </div>
  );
}

function subject(detail: MatchDetail): string {
  const r = detail.sanction_record;
  if (r.is_organization) return r.organization_name ?? r.record_id;
  return [r.first_name, r.middle_name, r.last_name, r.suffix].filter(Boolean).join(" ") || r.record_id;
}

/**
 * The sanction record as uploaded, with the fields the adjudicator cited
 * highlighted in place - the reviewer sees exactly which parts of the record
 * the AI's argument rests on.
 */
function RecordCard({ detail, citedFields }: { detail: MatchDetail; citedFields: Set<string> }) {
  const r = detail.sanction_record;
  const fields = fieldsFor(detail);
  const extra = Object.entries(r.raw ?? {}).filter(([, v]) => v !== null && v !== "");
  return (
    <Card>
      <CardHeader
        title="Sanction record"
        description={
          <>
            {r.sanction_type ? <>Type {r.sanction_type} · </> : null}
            Excluded {formatDate(r.exclusion_date)}
            {r.reinstatement_date ? <> · reinstated {formatDate(r.reinstatement_date)}</> : null}
            {!r.is_current ? " · a later upload replaced this version" : null}
          </>
        }
      />
      <dl className="grid gap-x-8 px-4 pb-3 md:grid-cols-2">
        {fields.map((field) => {
          const value = field.sanction(r);
          return (
            <Field key={field.key} label={field.label} mono={field.mono}>
              {value && citedFields.has(field.key) ? <mark className="cited">{value}</mark> : value}
            </Field>
          );
        })}
        {!r.is_organization && r.specialty ? <Field label="Specialty">{r.specialty}</Field> : null}
      </dl>
      {extra.length ? (
        <details className="border-t px-4 py-2 text-sm">
          <summary className="cursor-pointer text-xs text-muted">Original row as uploaded ({extra.length} columns)</summary>
          <dl className="mt-2 grid gap-x-8 md:grid-cols-2">
            {extra.map(([key, value]) => (
              <Field key={key} label={key} mono>
                {String(value)}
              </Field>
            ))}
          </dl>
        </details>
      ) : null}
    </Card>
  );
}

function InvestigationSkeleton() {
  return (
    <div className="space-y-4" aria-busy="true" aria-label="Loading the investigation">
      <Skeleton className="h-10 w-80" />
      <Skeleton className="h-14" />
      <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <div className="space-y-4">
          <Skeleton className="h-48" />
          <Skeleton className="h-80" />
        </div>
        <div className="space-y-4">
          <Skeleton className="h-32" />
          <Skeleton className="h-40" />
          <Skeleton className="h-64" />
        </div>
      </div>
    </div>
  );
}
