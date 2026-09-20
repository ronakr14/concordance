import { ArrowRight, GitCompare } from "lucide-react";
import { Link } from "react-router";

import { useRunDiff, useRuns } from "@/api/queries";
import type { DiffChange, Run, RunDiff } from "@/api/types";
import { EmptyState, ErrorState, PageHeader } from "@/components/states";
import { ConfidenceBadge, StatusBadge } from "@/components/status";
import { Select } from "@/components/ui/form";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui/surface";
import { formatDateTime, formatInt, formatPct } from "@/lib/format";
import { useSearchState } from "@/lib/useSearchState";

const DEFAULTS = { a: "", b: "", delta: "0.05" };

/** A run as the pickers label it: when it ran, what decided it, how big it was. */
function runLabel(run: Run): string {
  const when = formatDateTime(run.created_at);
  const config = run.scoring_config_version ?? "no config";
  return `${when} · ${config} · ${formatInt(run.records_total)} records`;
}

function Counts({ diff }: { diff: RunDiff }) {
  const tiles: { label: string; value: number; hint: string }[] = [
    { label: "Unchanged", value: diff.counts.unchanged, hint: "Same decision and same provider." },
    { label: "Changed decision", value: diff.counts.changed_decision, hint: "A different answer, or a different provider." },
    {
      label: "Moved confidence",
      value: diff.counts.changed_confidence,
      hint: `Same answer, confidence moved more than ${formatPct(diff.confidence_threshold, 0)}.`,
    },
    { label: "New", value: diff.counts.new, hint: "Decided by the later run only." },
    { label: "Removed", value: diff.counts.removed, hint: "Decided by the earlier run only." },
  ];
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
      {tiles.map((t) => (
        <div key={t.label} className="rounded-md bg-surface-2 px-3 py-2 ring-1 ring-border">
          <dt className="text-xs text-muted">{t.label}</dt>
          <dd className="tabular text-lg font-semibold text-ink">{formatInt(t.value)}</dd>
          <p className="text-xs text-muted">{t.hint}</p>
        </div>
      ))}
    </dl>
  );
}

/** The provenance that differs. This is the answer to "why did anything move". */
function ConfigDelta({ diff }: { diff: RunDiff }) {
  const rows = Object.entries(diff.config_delta);
  return (
    <Card>
      <CardHeader
        title="What differs between the runs"
        description={rows.length ? "Each of these can move a decision; a threshold change usually did." : undefined}
      />
      <CardBody>
        {rows.length === 0 ? (
          <p className="text-sm text-muted">
            Nothing: same config, engine, strategy, prompt and both snapshots. Any difference below is the engine
            behaving non-deterministically, which it should not.
          </p>
        ) : (
          <dl className="space-y-2 text-sm">
            {rows.map(([name, value]) => (
              <div key={name} className="grid grid-cols-[10rem_1fr] items-baseline gap-2">
                <dt className="text-xs text-muted">{name.replaceAll("_", " ")}</dt>
                <dd className="flex flex-wrap items-baseline gap-2 font-mono text-xs text-ink-2">
                  <span>{String(value.before ?? "—")}</span>
                  <ArrowRight className="size-3 text-muted" aria-hidden />
                  <span className="text-ink">{String(value.after ?? "—")}</span>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </CardBody>
    </Card>
  );
}

function ChangeTable({ title, description, rows, empty }: { title: string; description: string; rows: DiffChange[]; empty: string }) {
  return (
    <Card>
      <CardHeader title={title} description={description} />
      {rows.length === 0 ? (
        <CardBody>
          <p className="text-sm text-muted">{empty}</p>
        </CardBody>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-muted">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">Record</th>
                <th scope="col" className="px-3 py-2 font-medium">Before</th>
                <th scope="col" className="px-3 py-2 font-medium">After</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Confidence</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.record_id} className="border-t">
                  <th scope="row" className="px-3 py-2 text-left font-mono text-xs font-normal text-ink">
                    {row.result_after ? (
                      <Link className="underline decoration-border underline-offset-2 hover:decoration-ink" to={`/queue/${row.result_after}`}>
                        {row.record_id}
                      </Link>
                    ) : (
                      row.record_id
                    )}
                  </th>
                  <td className="px-3 py-2">
                    <StatusBadge status={row.decision_before} />
                    <span className="ml-2 font-mono text-xs text-muted">{row.provider_before ?? "—"}</span>
                  </td>
                  <td className="px-3 py-2">
                    <StatusBadge status={row.decision_after} />
                    <span className="ml-2 font-mono text-xs text-muted">{row.provider_after ?? "—"}</span>
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex items-center justify-end gap-2">
                      <ConfidenceBadge value={row.confidence_before} />
                      <ArrowRight className="size-3 shrink-0 text-muted" aria-hidden />
                      <ConfidenceBadge value={row.confidence_after} />
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

export function ComparePage() {
  const runs = useRuns();
  const { values, set } = useSearchState(DEFAULTS);
  const items = runs.data?.items ?? [];
  // Default to the two newest runs, newest as the "after" side.
  const fallbackB = items[0]?.id ?? "";
  const fallbackA = items[1]?.id ?? "";
  const a = values.a || fallbackA;
  const b = values.b || fallbackB;
  const delta = Number(values.delta) || 0.05;
  const diff = useRunDiff(a || null, b || null, delta);

  return (
    <>
      <PageHeader
        title="Compare runs"
        description="What two runs decided differently, and the provenance delta that explains it. Pick the earlier run on the left."
      />

      <Card className="mb-4">
        <CardBody className="flex flex-wrap items-end gap-4 py-3">
          <div className="min-w-64 flex-1">
            <label htmlFor="run-a" className="mb-1 block text-xs font-medium text-ink-2">
              Earlier run
            </label>
            <Select id="run-a" value={a} onChange={(e) => set({ a: e.target.value })}>
              <option value="">Pick a run</option>
              {items.map((r) => (
                <option key={r.id} value={r.id}>
                  {runLabel(r)}
                </option>
              ))}
            </Select>
          </div>
          <div className="min-w-64 flex-1">
            <label htmlFor="run-b" className="mb-1 block text-xs font-medium text-ink-2">
              Later run
            </label>
            <Select id="run-b" value={b} onChange={(e) => set({ b: e.target.value })}>
              <option value="">Pick a run</option>
              {items.map((r) => (
                <option key={r.id} value={r.id}>
                  {runLabel(r)}
                </option>
              ))}
            </Select>
          </div>
          <div className="w-44">
            <label htmlFor="delta" className="mb-1 block text-xs font-medium text-ink-2">
              Confidence move above
            </label>
            <Select id="delta" value={String(delta)} onChange={(e) => set({ delta: e.target.value })}>
              {["0.01", "0.05", "0.1", "0.25"].map((d) => (
                <option key={d} value={d}>
                  {formatPct(Number(d), 0)}
                </option>
              ))}
            </Select>
          </div>
        </CardBody>
      </Card>

      {runs.error ? (
        <Card>
          <ErrorState error={runs.error} onRetry={() => void runs.refetch()} />
        </Card>
      ) : items.length < 2 ? (
        <Card>
          <EmptyState title="Two runs are needed" body="Reconcile twice - after a config change, say - and the diff appears here." />
        </Card>
      ) : a === b ? (
        <Card>
          <EmptyState title="Pick two different runs" body="A run compared with itself has nothing to show." />
        </Card>
      ) : diff.error ? (
        <Card>
          <ErrorState error={diff.error} onRetry={() => void diff.refetch()} />
        </Card>
      ) : diff.isLoading || !diff.data ? (
        <Card>
          <CardBody>
            <Skeleton className="h-48" />
          </CardBody>
        </Card>
      ) : (
        <div className="space-y-4">
          <Card>
            <CardHeader
              title={
                <span className="inline-flex items-center gap-2">
                  <GitCompare className="size-4 text-muted" aria-hidden /> {formatInt(diff.data.counts.unchanged + diff.data.counts.changed_decision + diff.data.counts.changed_confidence)}{" "}
                  records decided by both runs
                </span>
              }
              description={`${runLabel(diff.data.run_a)} → ${runLabel(diff.data.run_b)}`}
            />
            <CardBody>
              <Counts diff={diff.data} />
            </CardBody>
          </Card>

          <ConfigDelta diff={diff.data} />

          <ChangeTable
            title="Decisions that changed"
            description="A different answer, or the same answer about a different provider. Open one to see the evidence behind it."
            rows={diff.data.changed_decision}
            empty="No record decided differently."
          />

          <ChangeTable
            title="Confidence that moved"
            description={`Same decision, confidence moved more than ${formatPct(diff.data.confidence_threshold, 0)}.`}
            rows={diff.data.changed_confidence}
            empty="No confidence moved beyond the threshold."
          />

          {diff.data.truncated ? (
            <p className="text-xs text-muted">Lists are cut to the first 200 rows; the counts above are complete.</p>
          ) : null}
        </div>
      )}
    </>
  );
}
