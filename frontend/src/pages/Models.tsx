import { CheckCircle2, GitBranch, Play, RefreshCw } from "lucide-react";
import { toast } from "sonner";

import { useActivateConfig, useActivations, useConfigs, useLabFeedback, useRetune, useStartFeedback } from "@/api/queries";
import type { ScoringConfig } from "@/api/types";
import { useIsAdmin } from "@/auth/AuthProvider";
import { EmptyState, ErrorState, PageHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Tip } from "@/components/ui/overlay";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui/surface";
import { formatDateTime, formatInt, formatPct } from "@/lib/format";
import { cn } from "@/lib/utils";

import { RoundsChart } from "./models/RoundsChart";

function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

type Holdout = { n?: number; precision?: number | null; recall?: number | null; missed?: number | null; review_share?: number | null };

const FITTED_FROM: Record<string, string> = {
  em: "EM fit",
  semi_supervised: "Retuned on labels",
  supervised: "Supervised",
  manual: "Manual",
};

/** Parent → new, on the same held-out labels. The number a person activates on. */
function HoldoutDelta({ config }: { config: ScoringConfig }) {
  const holdout = (config.metrics as { holdout?: { new?: Holdout; parent?: Holdout } }).holdout;
  if (!holdout?.new || !holdout.parent) return <span className="text-muted">—</span>;
  const rows: { label: string; key: keyof Holdout; better: "up" | "down" }[] = [
    { label: "Precision", key: "precision", better: "up" },
    { label: "Recall", key: "recall", better: "up" },
    { label: "Missed", key: "missed", better: "down" },
  ];
  return (
    <dl className="grid grid-cols-[auto_auto] gap-x-3 text-xs">
      {rows.map((r) => {
        const before = holdout.parent?.[r.key] as number | null | undefined;
        const after = holdout.new?.[r.key] as number | null | undefined;
        const gain = before != null && after != null ? (r.better === "up" ? after - before : before - after) : 0;
        return (
          <div key={r.key} className="contents">
            <dt className="text-muted">{r.label}</dt>
            <dd className="tabular text-ink-2">
              {formatPct(before ?? null)} → <span className={gain > 0.0005 ? "font-semibold text-ink" : undefined}>{formatPct(after ?? null)}</span>
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

function VersionsTable({ configs, onActivate, activating }: { configs: ScoringConfig[]; onActivate?: (c: ScoringConfig) => void; activating: string | null }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-left text-xs text-muted">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">Version</th>
            <th scope="col" className="px-3 py-2 font-medium">Lineage</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Labels</th>
            <th scope="col" className="px-3 py-2 font-medium">Held-out labels, parent → this</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Accept ≥</th>
            <th scope="col" className="px-3 py-2 text-right font-medium">Runs</th>
            <th scope="col" className="px-3 py-2 font-medium"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {configs.map((c) => {
            const labels = (c.metrics as { labels?: { total?: number } }).labels?.total;
            const recommendation = (c.metrics as { recommendation?: { activate?: boolean; reason?: string } }).recommendation;
            return (
              <tr key={c.id} className="border-t align-top">
                <td className="px-3 py-2">
                  <p className="font-mono text-xs text-ink">{c.version}</p>
                  <p className="text-xs text-muted">
                    {FITTED_FROM[c.fitted_from] ?? c.fitted_from} · {formatDateTime(c.fitted_at)}
                  </p>
                </td>
                <td className="px-3 py-2 text-xs text-ink-2">
                  {c.parent_version ? (
                    <span className="inline-flex items-center gap-1">
                      <GitBranch className="size-3.5 text-muted" aria-hidden />
                      from <span className="font-mono">{c.parent_version}</span>
                    </span>
                  ) : (
                    <span className="text-muted">root</span>
                  )}
                </td>
                <td className="tabular px-3 py-2 text-right">{labels != null ? formatInt(labels) : "—"}</td>
                <td className="px-3 py-2">
                  <HoldoutDelta config={c} />
                  {recommendation ? (
                    <p className={cn("mt-1 max-w-64 text-xs", recommendation.activate ? "text-good-text" : "text-muted")}>
                      {recommendation.activate ? "Worth activating" : "Not worth activating"}: {recommendation.reason}
                    </p>
                  ) : null}
                </td>
                <td className="tabular px-3 py-2 text-right">{c.t_auto_accept.toFixed(3)}</td>
                <td className="tabular px-3 py-2 text-right">
                  {formatInt(c.runs)}
                  {c.latest_run_at ? <span className="block text-xs text-muted">last {formatDateTime(c.latest_run_at)}</span> : null}
                </td>
                <td className="px-3 py-2 text-right">
                  {c.active ? (
                    <span className="inline-flex h-5.5 items-center gap-1 rounded-full bg-good/15 px-2 text-xs font-medium text-ink ring-1 ring-good/40 ring-inset">
                      <CheckCircle2 className="size-3.5 text-good-text" aria-hidden /> Active
                    </span>
                  ) : onActivate ? (
                    <Button size="sm" onClick={() => onActivate(c)} loading={activating === c.id}>
                      Activate
                    </Button>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

export function ModelsPage() {
  const isAdmin = useIsAdmin();
  const configs = useConfigs();
  const activations = useActivations();
  const feedback = useLabFeedback();
  const retune = useRetune();
  const activate = useActivateConfig();
  const startFeedback = useStartFeedback();

  const active = configs.data?.find((c) => c.active);
  const runRetune = () =>
    retune.mutate(
      { activate: false },
      {
        onSuccess: (out) =>
          toast.success(`Proposed ${out.config.version}`, {
            description: out.recommended
              ? `The held-out labels say activate it: ${out.verdict}.`
              : `It stays inactive: ${out.verdict}.`,
          }),
        onError: (error) => toast.error("Could not retune", { description: errorMessage(error) }),
      },
    );
  const onActivate = (c: ScoringConfig) =>
    activate.mutate(
      { id: c.id, reason: "activated from the Models page" },
      {
        onSuccess: () => toast.success(`${c.version} is active`, { description: "The next run scores with it. Earlier runs keep the config they used." }),
        onError: (error) => toast.error("Could not activate", { description: errorMessage(error) }),
      },
    );
  const runSimulation = () =>
    startFeedback.mutate(
      {},
      {
        onSuccess: () => toast.success("Review rounds queued", { description: "Five rounds of 200 labels on the newest sweep's datasets. A few minutes on the worker." }),
        onError: (error) => toast.error("Could not start the simulation", { description: errorMessage(error) }),
      },
    );

  const run = feedback.data?.run;
  const live = feedback.data?.live;
  const params = (run?.params ?? {}) as Record<string, number>;
  const simulateButton = isAdmin ? (
    <Tip content={live ? "A Lab experiment is already running" : "Label, retune and rescore five times on synthetic data"}>
      <span>
        <Button size="sm" onClick={runSimulation} loading={startFeedback.isPending} disabled={live != null}>
          <Play /> Simulate rounds
        </Button>
      </span>
    </Tip>
  ) : undefined;

  return (
    <>
      <PageHeader
        title="Models"
        description="Every scoring config the system has fitted, where each came from, and which one new runs score with. A retune proposes; activating is a separate, audited decision."
        actions={
          isAdmin ? (
            <Button variant="primary" onClick={runRetune} loading={retune.isPending}>
              <RefreshCw /> Retune on labels
            </Button>
          ) : undefined
        }
      />

      <Card className="mb-4">
        <CardHeader
          title="Versions"
          description={active ? `New runs score with ${active.version}.` : "No config is active yet; the first run activates the newest."}
        />
        {configs.error ? (
          <ErrorState error={configs.error} onRetry={() => void configs.refetch()} />
        ) : configs.isLoading ? (
          <CardBody>
            <Skeleton className="h-32" />
          </CardBody>
        ) : configs.data?.length ? (
          <VersionsTable
            configs={configs.data}
            onActivate={isAdmin ? onActivate : undefined}
            activating={activate.isPending ? (activate.variables?.id ?? null) : null}
          />
        ) : (
          <CardBody>
            <EmptyState title="No configs yet" body="Fit one with `concordance match fit`, or run a reconciliation to import the newest." />
          </CardBody>
        )}
      </Card>

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="xl:col-span-2">
          <RoundsChart
            rounds={feedback.data?.rounds ?? []}
            loading={feedback.isLoading}
            error={feedback.error}
            onRetry={() => void feedback.refetch()}
            actions={simulateButton}
          />
          {run ? (
            <p className="mt-2 text-xs text-muted">
              Starting config fitted at {formatPct(params.base_level, 0)} corruption, deployed on {formatPct(params.level, 0)}. {formatInt(params.per_round)} labels a
              round from a reviewer who is wrong {formatPct(params.noise, 0)} of the time; the retune is told {formatPct(params.assumed_noise ?? params.noise, 0)}.
              Finished {formatDateTime(run.finished_at)}.
            </p>
          ) : null}
          {live && live.kind === "feedback" ? (
            <p className="mt-2 text-xs text-muted" aria-live="polite">
              Simulation running · round {String((live.progress as { done?: number }).done ?? 0)} of {String((live.progress as { total?: number }).total ?? "?")}
            </p>
          ) : null}
        </div>

        <Card>
          <CardHeader title="Activation history" description="Which config was live when, and who switched it." />
          <CardBody>
            {activations.error ? (
              <ErrorState error={activations.error} onRetry={() => void activations.refetch()} />
            ) : activations.isLoading ? (
              <Skeleton className="h-32" />
            ) : (
              <ol className="space-y-3">
                {(activations.data ?? []).map((a) => (
                  <li key={a.id} className="text-sm">
                    <p className="font-mono text-xs text-ink">{a.version}</p>
                    <p className="text-xs text-muted">
                      {formatDateTime(a.created_at)} · {a.activated_by ? "by an admin" : "by the system"}
                    </p>
                    {a.reason ? <p className="text-xs text-ink-2">{a.reason}</p> : null}
                  </li>
                ))}
              </ol>
            )}
          </CardBody>
        </Card>
      </div>
    </>
  );
}
