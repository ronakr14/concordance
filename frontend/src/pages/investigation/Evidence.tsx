import { ArrowDownWideNarrow, Bot, CircleCheck, CircleDashed, CircleMinus, CircleX, ListOrdered } from "lucide-react";
import { useMemo, useState } from "react";

import type { MatchDetail, ProviderBrief } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/overlay";
import { formatBits, humanize } from "@/lib/format";
import { cn } from "@/lib/utils";

import { type FieldDef, type FieldTerm, fieldsFor, levelKind, providerName, termsFor } from "./fields";

const LEVEL_ICON = { agree: CircleCheck, partial: CircleMinus, disagree: CircleX, missing: CircleDashed };
const LEVEL_TONE = {
  agree: "text-good-text",
  partial: "text-warning-text",
  disagree: "text-critical-text",
  missing: "text-muted",
};

/**
 * The evidence table: the sanction record and one candidate side by side,
 * field by field, with the agreement level the comparator assigned, the m and
 * u probabilities behind it, and the weight in bits that field added to or
 * took from the match. The weights sum to the match weight; nothing on this
 * table is recomputed in the browser.
 */
export function EvidenceTable({
  detail,
  provider,
  cited,
}: {
  detail: MatchDetail;
  provider: ProviderBrief | null;
  cited: Set<string>;
}) {
  const [byStrength, setByStrength] = useState(true);
  const fields = fieldsFor(detail);
  const terms = useMemo(() => (provider ? termsFor(detail, provider.provider_id) : new Map<string, FieldTerm>()), [detail, provider]);
  const rows = useMemo(() => {
    const list = fields.map((field) => ({ field, term: terms.get(field.key) }));
    if (byStrength) list.sort((a, b) => Math.abs(b.term?.weight ?? 0) - Math.abs(a.term?.weight ?? 0));
    return list;
  }, [fields, terms, byStrength]);
  const scale = Math.max(1, ...rows.map((r) => Math.abs(r.term?.weight ?? 0)));
  const total = [...terms.values()].reduce((sum, t) => sum + t.weight, 0);

  return (
    <div>
      <div className="flex items-center justify-between gap-2 px-4 pb-2">
        <p className="text-xs text-muted">
          {provider ? (
            <>
              Sanction record against <span className="font-medium text-ink">{providerName(provider)}</span> (
              <span className="font-mono">{provider.provider_id}</span>)
            </>
          ) : (
            "No candidate to compare against."
          )}
        </p>
        <Button size="sm" variant="ghost" onClick={() => setByStrength((v) => !v)} aria-pressed={byStrength}>
          {byStrength ? <ArrowDownWideNarrow /> : <ListOrdered />}
          {byStrength ? "Strongest evidence first" : "Field order"}
        </Button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <caption className="sr-only">Field-by-field comparison with each field's weight of evidence</caption>
          <thead className="bg-surface-2 text-left text-xs text-muted">
            <tr>
              <th scope="col" className="px-4 py-2 font-medium">Field</th>
              <th scope="col" className="px-3 py-2 font-medium">Sanction record</th>
              <th scope="col" className="px-3 py-2 font-medium">Candidate</th>
              <th scope="col" className="px-3 py-2 font-medium">Agreement</th>
              <th scope="col" className="px-3 py-2 text-right font-medium whitespace-nowrap">
                <Tip content="m: how often true matches agree at this level. u: how often non-matches do. The weight is log₂(m/u).">
                  <span className="cursor-help underline decoration-dotted underline-offset-2">m / u</span>
                </Tip>
              </th>
              <th scope="col" className="w-56 px-4 py-2 font-medium">Weight (bits)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ field, term }) => (
              <EvidenceRow
                key={field.key}
                field={field}
                term={term}
                detail={detail}
                provider={provider}
                scale={scale}
                cited={provider ? cited.has(`${provider.provider_id}.${field.key}`) : false}
              />
            ))}
          </tbody>
          {provider ? (
            <tfoot>
              <tr className="border-t bg-surface-2/60 text-xs">
                <td className="px-4 py-2 font-medium text-ink" colSpan={5}>
                  Match weight (sum of fields)
                </td>
                <td className="tabular px-4 py-2 font-semibold text-ink">{formatBits(total)}</td>
              </tr>
            </tfoot>
          ) : null}
        </table>
      </div>
    </div>
  );
}

function EvidenceRow({
  field,
  term,
  detail,
  provider,
  scale,
  cited,
}: {
  field: FieldDef;
  term: FieldTerm | undefined;
  detail: MatchDetail;
  provider: ProviderBrief | null;
  scale: number;
  cited: boolean;
}) {
  const kind = term ? levelKind(term.level) : "missing";
  const Icon = LEVEL_ICON[kind];
  const left = field.sanction(detail.sanction_record);
  const right = provider ? field.provider(provider) : null;
  const value = (text: string | null) =>
    text ? (
      cited ? (
        <mark className="cited">{text}</mark>
      ) : (
        text
      )
    ) : (
      <span className="text-muted">—</span>
    );

  return (
    <tr className={cn("border-t", cited && "bg-warning/5")}>
      <th scope="row" className="px-4 py-2 text-left font-medium whitespace-nowrap text-ink-2">
        <span className="inline-flex items-center gap-1.5">
          {field.label}
          {cited ? (
            <Tip content="The adjudicator cited this field as evidence.">
              <Bot className="size-3.5 text-warning-text" aria-label="Cited by the adjudicator" />
            </Tip>
          ) : null}
        </span>
      </th>
      <td className={cn("px-3 py-2", field.mono && "font-mono text-xs")}>{value(left)}</td>
      <td className={cn("px-3 py-2", field.mono && "font-mono text-xs")}>{value(right)}</td>
      <td className="px-3 py-2">
        {term ? (
          <span className="inline-flex items-center gap-1.5 text-xs text-ink">
            <Icon className={cn("size-3.5", LEVEL_TONE[kind])} aria-hidden />
            {humanize(term.level.toLowerCase())}
          </span>
        ) : (
          <span className="text-xs text-muted">—</span>
        )}
      </td>
      <td className="tabular px-3 py-2 text-right text-xs whitespace-nowrap text-ink-2">
        {term?.m != null && term.u != null ? `${term.m.toFixed(3)} / ${term.u < 0.001 ? term.u.toExponential(0) : term.u.toFixed(3)}` : "—"}
      </td>
      <td className="px-4 py-2">{term ? <WeightBar weight={term.weight} scale={scale} /> : <span className="text-xs text-muted">—</span>}</td>
    </tr>
  );
}

/** A diverging bar from a centre line: blue for evidence of a match, red against. */
function WeightBar({ weight, scale }: { weight: number; scale: number }) {
  const width = (Math.abs(weight) / scale) * 50;
  return (
    <div className="flex items-center gap-2">
      <div className="relative h-2.5 flex-1" aria-hidden>
        <div className="absolute inset-y-0 left-1/2 w-px bg-axis" />
        <div
          className={cn("absolute inset-y-0", weight >= 0 ? "left-1/2 rounded-r-[3px] bg-diverge-pos" : "right-1/2 rounded-l-[3px] bg-diverge-neg")}
          style={{ width: `${width}%` }}
        />
      </div>
      <span className="tabular w-14 text-right text-xs text-ink">{formatBits(weight)}</span>
    </div>
  );
}
