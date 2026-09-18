import { Bot, CircleCheck, CircleQuestionMark, CircleX, GitCompare, History, Info } from "lucide-react";
import { Link } from "react-router";

import type { Band, MatchDetail } from "@/api/types";
import { ConfidenceBadge } from "@/components/status";
import { Card, CardBody, CardHeader } from "@/components/ui/surface";
import { formatInt, formatPct, formatUsd, humanize } from "@/lib/format";
import { cn } from "@/lib/utils";

import { providerName } from "./fields";

// --- recommendation -----------------------------------------------------------

type Recommendation = "APPROVE" | "REVIEW" | "REJECT";

const RECOMMENDATION: Record<Recommendation, { icon: typeof CircleCheck; chip: string; text: string }> = {
  APPROVE: { icon: CircleCheck, chip: "bg-good/10 ring-good/30", text: "Approve: the evidence supports this match." },
  REVIEW: {
    icon: CircleQuestionMark,
    chip: "bg-warning/12 ring-warning/40",
    text: "Review: the engine would not commit. Choose a candidate or reject.",
  },
  REJECT: { icon: CircleX, chip: "bg-critical/10 ring-critical/30", text: "Reject: no candidate is supported by the evidence." },
};

export function recommendationFor(detail: MatchDetail): Recommendation {
  if (detail.decision === "MATCH") return "APPROVE";
  if (detail.decision === "AMBIGUOUS") return "REVIEW";
  return "REJECT";
}

export function RecommendationBanner({ detail }: { detail: MatchDetail }) {
  const rec = recommendationFor(detail);
  const spec = RECOMMENDATION[rec];
  const reason = (detail.explanation as { reason?: string }).reason;
  return (
    <div className={cn("flex items-center gap-3 rounded-lg px-4 py-3 ring-1", spec.chip)} role="status">
      <spec.icon className="size-5 shrink-0 text-ink" aria-hidden />
      <div className="min-w-0">
        <p className="text-sm font-semibold text-ink">
          Recommendation: {rec}
        </p>
        <p className="text-xs text-ink-2">
          {spec.text}
          {reason ? <> Engine reason: <code className="font-mono">{reason}</code>.</> : null}
        </p>
      </div>
    </div>
  );
}

// --- Q5 banners -----------------------------------------------------------------

export function HistoryBanners({ detail }: { detail: MatchDetail }) {
  return (
    <>
      {detail.superseded_by ? (
        <div className="flex items-center gap-3 rounded-lg bg-surface-2 px-4 py-3 ring-1 ring-border-strong" role="status">
          <History className="size-5 shrink-0 text-muted" aria-hidden />
          <p className="text-sm text-ink">
            A later run replaced this decision. It is history, and cannot be reviewed.{" "}
            <Link className="font-medium text-accent hover:underline" to={`/queue/${detail.superseded_by}`}>
              Open the current result
            </Link>
          </p>
        </div>
      ) : null}
      {(detail.conflicting_cases ?? []).map((c) => (
        <div key={c.id} className="flex items-center gap-3 rounded-lg bg-serious/12 px-4 py-3 ring-1 ring-serious/40" role="alert">
          <GitCompare className="size-5 shrink-0 text-serious-text" aria-hidden />
          <p className="text-sm text-ink">
            Conflict: active case{" "}
            <Link className="font-mono font-medium text-accent hover:underline" to={`/cases/${c.id}`}>
              {c.case_number}
            </Link>{" "}
            was opened on an earlier decision about this record, against provider{" "}
            <span className="font-mono">{c.provider_id}</span>. This newer run disagrees. The case was not changed; a reviewer decides.
          </p>
        </div>
      ))}
    </>
  );
}

// --- confidence and its band ----------------------------------------------------

/**
 * The calibrated confidence against the thresholds of the config that decided
 * it: reject below `t_auto_reject`, accept at or above `t_auto_accept`, and
 * the grey band between - the only region an LLM is ever asked about.
 */
export function ConfidencePanel({ detail }: { detail: MatchDetail }) {
  const band = detail.band;
  const value = detail.calibrated_confidence;
  return (
    <Card>
      <CardHeader
        title="Calibrated confidence"
        description={band ? `Thresholds from scoring config ${band.version}` : "No scoring config recorded for this run"}
      />
      <CardBody className="space-y-3">
        <div className="flex items-baseline gap-2">
          <span className="text-3xl font-semibold tracking-tight text-ink">{formatPct(value)}</span>
          {band && value != null ? <span className="text-sm text-muted">{bandName(value, band)}</span> : null}
        </div>
        {band ? <BandTrack band={band} value={value} /> : null}
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <dt className="text-muted">Posterior</dt>
          <dd className="tabular text-right text-ink">{formatPct(detail.posterior, 2)}</dd>
          <dt className="text-muted">Match weight</dt>
          <dd className="tabular text-right text-ink">{detail.raw_match_weight?.toFixed(2) ?? "—"} bits</dd>
          <dt className="text-muted">Margin to runner-up</dt>
          <dd className="tabular text-right text-ink">{formatMargin((detail.explanation as { margin?: number }).margin)}</dd>
        </dl>
      </CardBody>
    </Card>
  );
}

function formatMargin(margin: number | undefined): string {
  return margin == null ? "—" : formatPct(margin);
}

function bandName(value: number, band: Band): string {
  if (value >= band.t_auto_accept) return "in the accept band";
  if (value < band.t_auto_reject) return "in the reject band";
  return "in the grey band";
}

function BandTrack({ band, value }: { band: Band; value: number | null }) {
  const reject = band.t_auto_reject * 100;
  const accept = band.t_auto_accept * 100;
  return (
    <div>
      <div className="relative h-3 overflow-hidden rounded-full bg-surface-3" role="img" aria-label={`Reject below ${formatPct(band.t_auto_reject)}, accept from ${formatPct(band.t_auto_accept)}`}>
        <div className="absolute inset-y-0 left-0 bg-critical/25" style={{ width: `${reject}%` }} />
        <div className="absolute inset-y-0 bg-warning/35" style={{ left: `${reject}%`, width: `${Math.max(accept - reject, 0.5)}%` }} />
        <div className="absolute inset-y-0 right-0 bg-good/25" style={{ left: `${accept}%` }} />
        {value != null ? (
          <div className="absolute top-1/2 size-3.5 -translate-x-1/2 -translate-y-1/2 rounded-full bg-ink ring-2 ring-surface" style={{ left: `${value * 100}%` }} />
        ) : null}
      </div>
      <div className="mt-1 flex justify-between text-[11px] text-muted">
        <span>Reject &lt; {formatPct(band.t_auto_reject)}</span>
        <span>Grey band</span>
        <span>Accept ≥ {formatPct(band.t_auto_accept)}</span>
      </div>
    </div>
  );
}

// --- candidate ranking -------------------------------------------------------------

export function CandidateList({
  detail,
  selected,
  onSelect,
}: {
  detail: MatchDetail;
  selected: string | null;
  onSelect: (providerId: string) => void;
}) {
  return (
    <Card>
      <CardHeader title="Candidates" description="Ranked by match weight. Select one to compare it." />
      {detail.candidates.length === 0 ? (
        <CardBody>
          <p className="flex items-start gap-2 text-sm text-muted">
            <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
            Blocking found no provider sharing a key with this record, so nothing was scored. Unmatched is the engine's answer.
          </p>
        </CardBody>
      ) : (
        <ul className="px-2 pb-2" role="listbox" aria-label="Candidates">
          {detail.candidates.map((c) => {
            const isSelected = c.provider_id === selected;
            const engineChoice = c.provider_id === detail.chosen_provider_id;
            const approved = c.provider_id === detail.approved_provider_id;
            return (
              <li key={c.provider_id}>
                <button
                  type="button"
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => onSelect(c.provider_id)}
                  className={cn(
                    "flex w-full items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-surface-2",
                    isSelected && "bg-accent-wash/50 ring-1 ring-accent/40",
                  )}
                >
                  <span className="tabular w-5 text-center text-xs text-muted">{c.rank}</span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-sm font-medium text-ink">{providerName(c.provider)}</span>
                    <span className="block truncate text-xs text-muted">
                      <span className="font-mono">{c.provider_id}</span>
                      {engineChoice ? " · engine's choice" : ""}
                      {approved ? " · approved" : ""}
                    </span>
                  </span>
                  <span className="text-right">
                    <ConfidenceBadge value={c.posterior} />
                    <span className="tabular block text-[11px] text-muted">{c.match_weight.toFixed(1)} bits</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

// --- the AI's explanation -------------------------------------------------------------

export function ExplanationPanel({ detail }: { detail: MatchDetail }) {
  const explanation = detail.explanation as { reason?: string; notes?: string[]; route?: string };
  const adjudication = detail.adjudication;
  const llm = detail.llm as
    | { provider?: string; model?: string; prompt_version?: string; prompt_tokens?: number; completion_tokens?: number; cost_usd?: number; latency_ms?: number }
    | null
    | undefined;

  return (
    <Card>
      <CardHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            <Bot className="size-4 text-muted" aria-hidden /> {adjudication ? "AI adjudication" : "Engine explanation"}
          </span>
        }
        description={
          adjudication
            ? "The adjudicator's answer, re-checked: every cited field was one it was shown."
            : detail.route === "llm"
              ? "Sent to the adjudicator, whose answer was refused or abstained. The engine's decision stands."
              : "Decided without an LLM. Only grey-band results are sent to one."
        }
      />
      <CardBody className="space-y-3 text-sm">
        {adjudication ? (
          <>
            <p className="leading-relaxed text-ink">{adjudication.reasoning || "No reasoning given."}</p>
            <div>
              <p className="mb-1 text-xs font-medium text-muted">Evidence cited</p>
              <div className="flex flex-wrap gap-1.5">
                {adjudication.evidence_cited.length ? (
                  adjudication.evidence_cited.map((key) => {
                    const [pid, field] = splitEvidence(key);
                    return (
                      <mark key={key} className="cited rounded-sm px-1.5 py-0.5 text-xs">
                        <span className="font-mono">{pid}</span> · {humanize(field)}
                      </mark>
                    );
                  })
                ) : (
                  <span className="text-xs text-muted">None</span>
                )}
              </div>
            </div>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
              <dt className="text-muted">Model's decision</dt>
              <dd className="text-right text-ink">{adjudication.decision}</dd>
              <dt className="text-muted">Model's confidence</dt>
              <dd className="tabular text-right text-ink">{formatPct(adjudication.confidence)}</dd>
            </dl>
          </>
        ) : (
          <ul className="list-disc space-y-1 pl-5 text-ink-2">
            {explanation.reason ? (
              <li>
                Reason: <code className="font-mono text-xs">{explanation.reason}</code>
              </li>
            ) : null}
            {(explanation.notes ?? []).map((note, i) => (
              <li key={i}>{note}</li>
            ))}
          </ul>
        )}
        {llm ? (
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1 border-t pt-2 text-xs">
            <dt className="text-muted">Model</dt>
            <dd className="truncate text-right text-ink">
              {llm.provider} · {llm.model}
            </dd>
            <dt className="text-muted">Prompt version</dt>
            <dd className="text-right font-mono text-ink">{llm.prompt_version}</dd>
            <dt className="text-muted">Tokens</dt>
            <dd className="tabular text-right text-ink">{formatInt((llm.prompt_tokens ?? 0) + (llm.completion_tokens ?? 0))}</dd>
            <dt className="text-muted">Cost</dt>
            <dd className="tabular text-right text-ink">{formatUsd(llm.cost_usd)}</dd>
          </dl>
        ) : null}
      </CardBody>
    </Card>
  );
}

/** `P0001487.last_name` -> [`P0001487`, `last_name`]. Provider ids contain no dots. */
export function splitEvidence(key: string): [string, string] {
  const dot = key.indexOf(".");
  return dot < 0 ? [key, ""] : [key.slice(0, dot), key.slice(dot + 1)];
}
