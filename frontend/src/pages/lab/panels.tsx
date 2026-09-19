import { FlaskConical, Info } from "lucide-react";
import type { ReactNode } from "react";

import type { LabCalibration, LabCell, LabLlmLevel, LabResults, LlmStrategy } from "@/api/types";
import { useChartTheme } from "@/components/ChartCard";
import { EmptyState } from "@/components/states";
import { Tip } from "@/components/ui/overlay";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui/surface";
import { formatCompact, formatInt, formatPct, formatUsd, humanize } from "@/lib/format";

import { cellAt, pct, STRATEGIES, type StrategyKey } from "./model";

// --- the selected level, in numbers -----------------------------------------------

function Figure({ label, value, detail }: { label: string; value: ReactNode; detail?: ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-muted">{label}</p>
      <p className="mt-0.5 text-2xl font-semibold tracking-tight text-ink">{value}</p>
      {detail ? <p className="mt-0.5 text-xs text-muted">{detail}</p> : null}
    </div>
  );
}

/**
 * The confidence scale with the two thresholds on it. Everything left of the
 * reject threshold is decided "no match", everything right of accept is decided
 * "match", and only the band between reaches a person (or the LLM).
 */
function GreyBand({ entry }: { entry: LabCalibration | undefined }) {
  const reject = entry?.t_auto_reject;
  const accept = entry?.t_auto_accept;
  if (reject == null || accept == null) {
    return <p className="text-sm text-muted">No thresholds were fitted at this level.</p>;
  }
  const lo = Math.min(reject, accept);
  const hi = Math.max(reject, accept);
  return (
    <div>
      <div
        className="relative flex h-7 overflow-hidden rounded-md ring-1 ring-border"
        role="img"
        aria-label={`Auto-reject below ${formatPct(lo)}, review between ${formatPct(lo)} and ${formatPct(hi)}, auto-accept above ${formatPct(hi)}`}
      >
        <div className="grid place-items-center overflow-hidden bg-surface-2 text-[11px] whitespace-nowrap text-ink-2" style={{ width: `${lo * 100}%` }}>
          {lo > 0.24 ? "auto no-match" : null}
        </div>
        <div className="grid place-items-center overflow-hidden bg-accent-wash text-[11px] font-medium whitespace-nowrap text-ink" style={{ width: `${(hi - lo) * 100}%` }}>
          {hi - lo > 0.1 ? "review" : null}
        </div>
        <div className="grid flex-1 place-items-center overflow-hidden bg-surface-2 text-[11px] whitespace-nowrap text-ink-2">{1 - hi > 0.2 ? "auto match" : null}</div>
      </div>
      <div className="tabular mt-1 flex justify-between text-[11px] text-muted">
        <span>0%</span>
        <span>
          reject &lt; {formatPct(lo, 2)} · accept ≥ {formatPct(hi, 2)}
        </span>
        <span>100%</span>
      </div>
    </div>
  );
}

export function LevelSummary({
  results,
  level,
  calibration,
  loading,
}: {
  results: LabResults | undefined;
  level: number;
  calibration: LabCalibration | undefined;
  loading: boolean;
}) {
  const cells = results?.cells ?? [];
  const prob = cellAt(cells, level, "probabilistic");
  const fuzzy = cellAt(cells, level, "fuzzy");
  const lift = prob?.f1 != null && fuzzy?.f1 != null ? prob.f1 - fuzzy.f1 : null;
  return (
    <Card>
      <CardHeader title={`At ${pct(level)} corruption`} description="The probabilistic engine at the dial's level" />
      <CardBody className="space-y-5">
        {loading ? (
          <Skeleton className="h-40" />
        ) : !prob ? (
          <p className="text-sm text-muted">This level was not measured.</p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-4">
              <Figure
                label="F1"
                value={formatPct(prob.f1)}
                detail={lift != null ? `${lift >= 0 ? "+" : "−"}${(Math.abs(lift) * 100).toFixed(1)} points over fuzzy` : undefined}
              />
              <Figure label="Blocking recall" value={formatPct(prob.blocking_recall)} detail="The ceiling on recall" />
              <Figure label="Grey band" value={formatPct(prob.grey_band_fraction)} detail="Of records sent to review" />
              <Figure
                label="Calibration error"
                value={calibration?.after.ece != null ? calibration.after.ece.toFixed(3) : "—"}
                detail={
                  calibration?.before.ece != null
                    ? `ECE, from ${calibration.before.ece.toFixed(3)} before · Brier ${calibration.after.brier?.toFixed(3) ?? "—"}`
                    : "ECE on the holdout"
                }
              />
            </div>
            <div>
              <p className="mb-1.5 text-xs text-muted">Decision thresholds ({calibration?.model ?? "individual"} model)</p>
              <GreyBand entry={calibration} />
            </div>
          </>
        )}
      </CardBody>
    </Card>
  );
}

// --- the LLM cost panel -------------------------------------------------------------

const ROWS: { key: "probabilistic" | "routed" | "everything"; label: string; hint: string }[] = [
  { key: "probabilistic", label: "Engine alone", hint: "No model calls; exact on the full dataset." },
  { key: "routed", label: "Routed (grey band only)", hint: "What the system does." },
  { key: "everything", label: "LLM on everything", hint: "Every record with a candidate goes to the model." },
];

/** F1 as a dot with its 95% interval, on a shared scale, one row per strategy. */
function IntervalPlot({ strategies }: { strategies: Record<string, LlmStrategy> }) {
  const t = useChartTheme();
  const values = ROWS.flatMap((r) => {
    const s = strategies[r.key];
    return s ? [s.f1, ...(s.interval?.f1 ?? [])] : [];
  });
  const min = Math.max(0, Math.floor((Math.min(...values) - 0.02) * 20) / 20);
  const max = Math.min(1, Math.ceil((Math.max(...values) + 0.01) * 20) / 20);
  const x = (v: number) => `${((v - min) / (max - min || 1)) * 100}%`;
  return (
    <div className="space-y-2.5">
      {ROWS.map((row) => {
        const s = strategies[row.key];
        if (!s) return null;
        const lo = s.interval?.f1[0] ?? s.f1;
        const hi = s.interval?.f1[1] ?? s.f1;
        const color = row.key === "probabilistic" ? t.series1 : row.key === "routed" ? t.series2 : t.muted;
        return (
          <div key={row.key} className="grid grid-cols-[10.5rem_1fr_4.5rem] items-center gap-3">
            <Tip content={row.hint}>
              <span className="truncate text-xs text-ink-2">{row.label}</span>
            </Tip>
            <div className="relative h-5">
              <div className="absolute inset-x-0 top-1/2 h-px bg-grid" />
              {s.interval ? (
                <div className="absolute top-1/2 h-0.5 -translate-y-1/2 rounded-full" style={{ left: x(lo), width: `calc(${x(hi)} - ${x(lo)})`, background: color }} />
              ) : null}
              <div
                className="absolute top-1/2 size-3 -translate-x-1/2 -translate-y-1/2 rounded-full ring-2 ring-surface"
                style={{ left: x(s.f1), background: color }}
                aria-hidden
              />
            </div>
            <span className="tabular text-right text-xs font-medium text-ink">
              {formatPct(s.f1)}
              {s.interval ? <span className="block text-[10px] font-normal text-muted">{`${formatPct(lo)}–${formatPct(hi)}`}</span> : null}
            </span>
          </div>
        );
      })}
      <div className="grid grid-cols-[10.5rem_1fr_4.5rem] gap-3">
        <span />
        <div className="tabular flex justify-between text-[10px] text-muted">
          <span>{formatPct(min, 0)}</span>
          <span>F1, with 95% interval</span>
          <span>{formatPct(max, 0)}</span>
        </div>
      </div>
    </div>
  );
}

function SpendColumn({ title, spend, accent }: { title: string; spend: LabLlmLevel["cost"]["routed"]; accent?: boolean }) {
  return (
    <div className={accent ? "rounded-md bg-accent-wash/50 p-3 ring-1 ring-border" : "rounded-md p-3 ring-1 ring-border"}>
      <p className="text-xs font-medium text-ink-2">{title}</p>
      <dl className="mt-2 space-y-1 text-sm">
        <div className="flex justify-between gap-2">
          <dt className="text-muted">Model calls</dt>
          <dd className="tabular text-ink">{formatInt(spend.calls)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-muted">Tokens</dt>
          <dd className="tabular text-ink">{formatCompact(spend.tokens)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-muted">Cost per run</dt>
          <dd className="tabular font-medium text-ink">{formatUsd(spend.usd)}</dd>
        </div>
      </dl>
    </div>
  );
}

export function LlmCostPanel({
  results,
  level,
  entry,
  measured,
  action,
  loading,
}: {
  results: LabResults | undefined;
  level: number;
  entry: LabLlmLevel | undefined;
  measured: number[];
  action?: ReactNode;
  loading: boolean;
}) {
  const run = results?.llm_run;
  const header = (
    <CardHeader
      title="LLM cost versus an LLM-on-everything baseline"
      description={
        entry
          ? `At ${pct(entry.level ?? level)} corruption${entry.level !== level ? ` (nearest sampled level to ${pct(level)})` : ""}`
          : "Routing only the grey band to the model, compared with sending it every record"
      }
      actions={action}
    />
  );
  if (loading) {
    return (
      <Card>
        {header}
        <CardBody>
          <Skeleton className="h-48" />
        </CardBody>
      </Card>
    );
  }
  if (!entry) {
    return (
      <Card>
        {header}
        <CardBody>
          <EmptyState
            title={run?.status === "FAILED" ? "The last LLM sample failed" : "No LLM sample yet"}
            body={
              run?.status === "FAILED"
                ? (run.error ?? "See the worker log.")
                : results?.llm_enabled === false
                  ? "The LLM is disabled (LLM_ENABLED=false). Enable it to measure what routing saves."
                  : "An admin can run a sample on this sweep: real model calls on a stratified sample at three levels."
            }
          />
        </CardBody>
      </Card>
    );
  }

  const saving = entry.cost.saving;
  const routed = entry.strategies.routed;
  const everything = entry.strategies.everything;
  return (
    <Card>
      {header}
      <CardBody className="space-y-5">
        <div className="grid gap-5">
          <div className="space-y-3">
            <div className="flex items-baseline gap-2">
              <FlaskConical className="size-4 self-center text-muted" aria-hidden />
              <p className="text-sm text-ink-2">
                Routing makes{" "}
                <span className="font-semibold text-ink">{formatPct(saving.calls ?? null, 0)} fewer model calls</span> and costs{" "}
                <span className="font-semibold text-ink">{formatPct(saving.usd ?? null, 0)} less</span>.
              </p>
            </div>
            <div className="grid grid-cols-2 gap-3">
              <SpendColumn title="Routed" spend={entry.cost.routed} accent />
              <SpendColumn title="LLM on everything" spend={entry.cost.everything} />
            </div>
          </div>
          <div className="space-y-3">
            <IntervalPlot strategies={entry.strategies} />
            {routed && everything ? (
              <p className="text-xs text-muted">
                Records left for a person: {formatInt(Math.round(routed.review))} routed, {formatInt(Math.round(everything.review))} with the model deciding
                everything.
              </p>
            ) : null}
          </div>
        </div>
        <p className="flex gap-1.5 border-t pt-3 text-xs text-muted">
          <Info className="mt-px size-3.5 shrink-0" aria-hidden />
          <span>
            Extrapolated from {formatInt(entry.sample.grey)} grey-band and {formatInt(entry.sample.decided)} decided records sampled from{" "}
            {formatInt(entry.population.records)}; intervals are a stratified bootstrap. Priced at {entry.cost.price_model}
            {results?.price ? ` ($${results.price.prompt_per_million} / $${results.price.completion_per_million} per million tokens)` : ""} — a
            placeholder price, verify before quoting. Sampled at {measured.map(pct).join(", ")}.
            {entry.sample.failed ? ` ${entry.sample.failed} call(s) never completed and were dropped.` : ""}
          </span>
        </p>
      </CardBody>
    </Card>
  );
}

// --- per-scenario accuracy --------------------------------------------------------------

export function ScenarioTable({ cells, level, visible }: { cells: LabCell[]; level: number; visible: Set<StrategyKey> }) {
  const t = useChartTheme();
  const shown = STRATEGIES.filter((s) => s.key !== "probabilistic_llm" && visible.has(s.key));
  const byStrategy = new Map(shown.map((s) => [s.key, cellAt(cells, level, s.key)]));
  const names = [...new Set(shown.flatMap((s) => byStrategy.get(s.key)?.scenarios.map((x) => x.scenario) ?? []))].sort();
  return (
    <Card>
      <CardHeader
        title="Accuracy by scenario"
        description={`Share of records decided correctly at ${pct(level)} corruption. An aggregate F1 hides which capability broke; this does not.`}
      />
      <CardBody className="overflow-x-auto">
        {names.length === 0 ? (
          <p className="text-sm text-muted">No scenario breakdown at this level.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th scope="col" className="px-2 py-1.5 font-medium">
                  Scenario
                </th>
                <th scope="col" className="px-2 py-1.5 text-right font-medium">
                  Records
                </th>
                {shown.map((s) => (
                  <th key={s.key} scope="col" className="min-w-36 px-2 py-1.5 font-medium">
                    <span className="inline-flex items-center gap-1.5">
                      <span className="h-0.5 w-3 rounded-full" style={{ background: t[s.color] }} aria-hidden />
                      {s.label}
                    </span>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {names.map((name) => {
                const n = shown.map((s) => byStrategy.get(s.key)?.scenarios.find((x) => x.scenario === name)?.n).find((v) => v != null);
                return (
                  <tr key={name} className="border-t">
                    <th scope="row" className="px-2 py-1.5 text-left font-normal text-ink">
                      {humanize(name)}
                    </th>
                    <td className="tabular px-2 py-1.5 text-right text-ink-2">{formatInt(n ?? null)}</td>
                    {shown.map((s) => {
                      const accuracy = byStrategy.get(s.key)?.scenarios.find((x) => x.scenario === name)?.accuracy;
                      return (
                        <td key={s.key} className="px-2 py-1.5">
                          <div className="flex items-center gap-2">
                            <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-2">
                              <div className="h-full rounded-full" style={{ width: `${(accuracy ?? 0) * 100}%`, background: t[s.color] }} />
                            </div>
                            <span className="tabular w-12 text-right text-xs text-ink">{formatPct(accuracy ?? null)}</span>
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </CardBody>
    </Card>
  );
}
