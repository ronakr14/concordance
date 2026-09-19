import { FlaskConical, Play } from "lucide-react";
import { useMemo } from "react";
import { toast } from "sonner";

import { useLabResults, useStartLlm, useStartSweep } from "@/api/queries";
import type { LabRun } from "@/api/types";
import { useIsAdmin } from "@/auth/AuthProvider";
import { useChartTheme } from "@/components/ChartCard";
import { EmptyState, ErrorState, PageHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/form";
import { Tip } from "@/components/ui/overlay";
import { Card, CardBody } from "@/components/ui/surface";
import { formatDateTime, formatInt } from "@/lib/format";
import { useSearchState } from "@/lib/useSearchState";
import { cn } from "@/lib/utils";

import { ReliabilityChart, RobustnessChart } from "./lab/charts";
import {
  calibrationAt,
  curveRows,
  levelsOf,
  llmAt,
  llmPoints,
  type Metric,
  METRICS,
  nearest,
  pct,
  STRATEGIES,
  type StrategyKey,
} from "./lab/model";
import { LevelSummary, LlmCostPanel, ScenarioTable } from "./lab/panels";

const DEFAULTS = { level: "0.5", metric: "f1", model: "individual", hide: "" };

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

/** Whichever experiment is queued or running, and how far it has got. */
function LiveBanner({ live }: { live: LabRun }) {
  const p = live.progress as { done?: number; total?: number; level?: number; levels_done?: number; levels_total?: number };
  const sweep = live.kind === "sweep";
  const fraction = sweep
    ? p.total
      ? (p.done ?? 0) / p.total
      : 0
    : p.levels_total
      ? ((p.levels_done ?? 0) + (p.total ? (p.done ?? 0) / p.total : 0)) / p.levels_total
      : 0;
  const detail = sweep
    ? p.total
      ? `${p.done ?? 0} of ${p.total} levels`
      : "starting"
    : p.levels_total
      ? `level ${Math.min((p.levels_done ?? 0) + 1, p.levels_total)} of ${p.levels_total}${p.total ? ` · ${p.done ?? 0} of ${p.total} model calls` : ""}`
      : "starting";
  return (
    <Card className="mb-4" aria-live="polite">
      <CardBody className="flex flex-wrap items-center gap-3 py-3">
        <FlaskConical className="size-4 text-accent" aria-hidden />
        <p className="text-sm text-ink">
          {sweep ? "Corruption sweep" : "LLM sample"} {live.status === "QUEUED" ? "queued" : "running"}
          <span className="text-muted"> · {detail}</span>
        </p>
        <div
          className="h-1.5 min-w-40 flex-1 overflow-hidden rounded-full bg-accent-wash"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(fraction * 100)}
          aria-label="Experiment progress"
        >
          <div className="h-full rounded-full bg-series-1 transition-[width]" style={{ width: `${fraction * 100}%` }} />
        </div>
        <p className="text-xs text-muted">The charts below keep showing the last completed sweep until this one finishes.</p>
      </CardBody>
    </Card>
  );
}

function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: { key: T; label: string }[];
  onChange: (value: T) => void;
}) {
  return (
    <div role="radiogroup" aria-label={label} className="inline-flex rounded-md bg-surface-2 p-0.5 ring-1 ring-border">
      {options.map((o) => (
        <button
          key={o.key}
          type="button"
          role="radio"
          aria-checked={value === o.key}
          onClick={() => onChange(o.key)}
          className={cn(
            "h-7 rounded-[5px] px-2.5 text-xs font-medium transition-colors",
            value === o.key ? "bg-surface text-ink shadow-sm ring-1 ring-border" : "text-ink-2 hover:text-ink",
          )}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function LabPage() {
  const results = useLabResults();
  const isAdmin = useIsAdmin();
  const startSweep = useStartSweep();
  const startLlm = useStartLlm();
  const t = useChartTheme();
  const { values, set } = useSearchState(DEFAULTS);

  const data = results.data;
  const levels = useMemo(() => levelsOf(data), [data]);
  const level = nearest(levels, Number(values.level)) ?? Number(values.level);
  const metric = (METRICS.some((m) => m.key === values.metric) ? values.metric : "f1") as Metric;
  const hidden = new Set(values.hide.split(",").filter(Boolean));
  const visible = new Set(STRATEGIES.map((s) => s.key).filter((k) => !hidden.has(k)));
  const models = [...new Set((data?.calibration ?? []).map((c) => c.model))].sort();
  const model = models.includes(values.model) ? values.model : (models[0] ?? "individual");

  const rows = useMemo(() => curveRows(data?.cells ?? [], metric), [data, metric]);
  const llm = useMemo(() => llmPoints(data?.llm ?? [], metric), [data, metric]);
  const llmLevels = llm.map((p) => p.level);
  const llmLevel = nearest(llmLevels, level);
  const llmEntry = llmLevel != null ? llmAt(data?.llm ?? [], llmLevel) : undefined;
  const calibration = calibrationAt(data?.calibration ?? [], level, model);
  const live = data?.live ?? null;

  const toggle = (key: StrategyKey) => {
    const next = new Set(hidden);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    set({ hide: [...next].sort().join(",") });
  };

  const runSweep = () =>
    startSweep.mutate(
      {},
      {
        onSuccess: () => toast.success("Sweep queued", { description: "Ten levels by three strategies. A worker takes a few minutes." }),
        onError: (error) => toast.error("Could not start the sweep", { description: errorMessage(error) }),
      },
    );
  const runLlm = () =>
    startLlm.mutate(
      { sweep_id: data?.sweep?.id ?? null },
      {
        onSuccess: () => toast.success("LLM sample queued", { description: "Real model calls on a stratified sample at 30%, 50% and 70%." }),
        onError: (error) => toast.error("Could not start the LLM sample", { description: errorMessage(error) }),
      },
    );

  const llmBlocked = !data?.sweep ? "Run a sweep first" : data.llm_enabled ? (live ? "An experiment is already running" : null) : "The LLM is disabled (LLM_ENABLED=false)";
  const llmButton = isAdmin ? (
    <Tip content={llmBlocked ?? "Sample 100 grey-band and 100 decided records per level"}>
      <span>
        <Button size="sm" onClick={runLlm} loading={startLlm.isPending} disabled={llmBlocked != null}>
          <Play /> Run LLM sample
        </Button>
      </span>
    </Tip>
  ) : undefined;

  const sweep = data?.sweep;
  return (
    <>
      <PageHeader
        title="Lab"
        description={
          sweep ? (
            <>
              How the engine holds up as the data gets worse, measured against ground truth. Sweep of{" "}
              {formatInt(Number(sweep.params.providers))} providers × {formatInt(Number(sweep.params.sanctions))} sanction records, seed{" "}
              {String(sweep.params.seed)}, finished {formatDateTime(sweep.finished_at)}
              {sweep.summary.seconds ? ` in ${Math.round(Number(sweep.summary.seconds))} s` : ""}.
            </>
          ) : (
            "How the engine holds up as the data gets worse, measured against ground truth."
          )
        }
        actions={
          isAdmin ? (
            <Button variant="primary" onClick={runSweep} loading={startSweep.isPending} disabled={live != null}>
              <Play /> Run sweep
            </Button>
          ) : undefined
        }
      />

      {live ? <LiveBanner live={live} /> : null}

      {results.error ? (
        <Card>
          <ErrorState error={results.error} onRetry={() => void results.refetch()} />
        </Card>
      ) : !results.isLoading && !sweep ? (
        <Card>
          <EmptyState
            title="No sweep yet"
            body={
              isAdmin
                ? "Run a sweep to measure every strategy at every corruption level from 0% to 90%. It takes a few minutes on the worker."
                : "An admin can run a sweep: every strategy at every corruption level from 0% to 90%."
            }
          />
        </Card>
      ) : (
        <>
          <Card className="mb-4">
            <CardBody className="flex flex-wrap items-center gap-x-6 gap-y-3 py-3">
              <div className="flex min-w-64 flex-1 items-center gap-3">
                <label htmlFor="corruption" className="text-xs font-medium whitespace-nowrap text-ink-2">
                  Corruption
                </label>
                <input
                  id="corruption"
                  type="range"
                  min={levels[0] ?? 0}
                  max={levels[levels.length - 1] ?? 0.9}
                  step={0.1}
                  value={level}
                  onChange={(e) => set({ level: String(Number(e.target.value).toFixed(1)) })}
                  list="corruption-levels"
                  className="flex-1 accent-(--series-1)"
                  aria-valuetext={`${pct(level)} of fields corrupted`}
                />
                <datalist id="corruption-levels">
                  {levels.map((l) => (
                    <option key={l} value={l} />
                  ))}
                </datalist>
                <span className="tabular w-10 text-right text-lg font-semibold text-ink">{pct(level)}</span>
              </div>
              <div role="group" aria-label="Strategies shown" className="flex flex-wrap gap-1.5">
                {STRATEGIES.map((s) => {
                  const on = visible.has(s.key);
                  return (
                    <button
                      key={s.key}
                      type="button"
                      aria-pressed={on}
                      onClick={() => toggle(s.key)}
                      className={cn(
                        "inline-flex h-7 items-center gap-1.5 rounded-full px-2.5 text-xs ring-1 transition-colors",
                        on ? "bg-surface text-ink ring-border-strong" : "text-muted ring-border hover:text-ink-2",
                      )}
                    >
                      <svg width="14" height="4" aria-hidden className={on ? "" : "opacity-40"}>
                        <line x1="0" y1="2" x2="14" y2="2" stroke={t[s.color]} strokeWidth="2" strokeDasharray={s.dash} strokeLinecap="round" />
                      </svg>
                      {s.label}
                    </button>
                  );
                })}
              </div>
              <Segmented label="Metric" value={metric} options={METRICS} onChange={(m) => set({ metric: m })} />
              {models.length > 1 ? (
                <Select className="w-36" aria-label="Model" value={model} onChange={(e) => set({ model: e.target.value })}>
                  {models.map((m) => (
                    <option key={m} value={m}>
                      {m === "organization" ? "Organization model" : "Individual model"}
                    </option>
                  ))}
                </Select>
              ) : null}
            </CardBody>
          </Card>

          <div className="grid gap-4 xl:grid-cols-3">
            <div className="xl:col-span-2">
              <RobustnessChart
                rows={rows}
                llm={llm}
                metric={metric}
                visible={visible}
                level={level}
                onPick={(l) => set({ level: l.toFixed(1) })}
                loading={results.isLoading}
                error={null}
                onRetry={() => void results.refetch()}
              />
            </div>
            <LevelSummary results={data} level={level} calibration={calibration} loading={results.isLoading} />
          </div>

          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <ReliabilityChart entry={calibration} level={level} model={model} loading={results.isLoading} error={null} onRetry={() => void results.refetch()} />
            <LlmCostPanel results={data} level={level} entry={llmEntry} measured={llmLevels} action={llmButton} loading={results.isLoading} />
          </div>

          <div className="mt-4">
            <ScenarioTable cells={data?.cells ?? []} level={level} visible={visible} />
          </div>
        </>
      )}
    </>
  );
}
