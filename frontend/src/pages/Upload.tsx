import { ArrowRight, CircleAlert, CircleCheck, FileSpreadsheet, Play, Upload as UploadIcon } from "lucide-react";
import { type DragEvent, type FormEvent, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";

import { ApiError } from "@/api/client";
import { useCommitUpload, useInspectUpload, useRuns, useStartRun } from "@/api/queries";
import type { CommitResult, Inspection, Run } from "@/api/types";
import { PageHeader } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { Button } from "@/components/ui/button";
import { FieldError, Input, Label, Select } from "@/components/ui/form";
import { Card, CardBody, CardHeader } from "@/components/ui/surface";
import { formatInt, formatUsd, humanize } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Required: a name, as a person or as an organization. Everything else is optional. */
const NAME_FIELDS = ["last_name", "organization_name"];
const MAX_BYTES = 25 * 1024 * 1024;

type Step = "pick" | "map" | "done";

export function UploadPage() {
  const [params] = useSearchParams();
  const watching = params.get("run");
  const [step, setStep] = useState<Step>(watching ? "done" : "pick");
  const [inspection, setInspection] = useState<Inspection | null>(null);
  const [committed, setCommitted] = useState<CommitResult | null>(null);

  return (
    <>
      <PageHeader title="Upload a sanction list" description="Two steps: the file is inspected and a column mapping proposed; nothing is ingested until you confirm it." />
      <Stepper step={step} />
      {step === "pick" ? (
        <PickStep
          onInspected={(result) => {
            setInspection(result);
            setStep("map");
          }}
        />
      ) : step === "map" && inspection ? (
        <MapStep
          inspection={inspection}
          onCommitted={(result) => {
            setCommitted(result);
            setStep("done");
          }}
          onRestart={() => setStep("pick")}
        />
      ) : (
        <DoneStep committed={committed} fileId={committed?.file_id ?? params.get("file")} runId={watching} />
      )}
    </>
  );
}

function Stepper({ step }: { step: Step }) {
  const steps: [Step, string][] = [
    ["pick", "Choose a file"],
    ["map", "Map columns"],
    ["done", "Ingest and reconcile"],
  ];
  const index = steps.findIndex(([s]) => s === step);
  return (
    <ol className="mb-4 flex flex-wrap items-center gap-2 text-sm" aria-label="Upload progress">
      {steps.map(([key, label], i) => (
        <li key={key} className="flex items-center gap-2" aria-current={i === index ? "step" : undefined}>
          <span
            className={cn(
              "grid size-6 place-items-center rounded-full text-xs font-semibold ring-1",
              i < index ? "bg-good-text text-white ring-good-text" : i === index ? "bg-accent text-accent-fg ring-accent" : "bg-surface text-muted ring-border-strong",
            )}
          >
            {i < index ? <CircleCheck className="size-3.5" /> : i + 1}
          </span>
          <span className={i === index ? "font-medium text-ink" : "text-muted"}>{label}</span>
          {i < steps.length - 1 ? <ArrowRight className="size-4 text-muted" aria-hidden /> : null}
        </li>
      ))}
    </ol>
  );
}

// --- step 1 ------------------------------------------------------------------------

function PickStep({ onInspected }: { onInspected: (inspection: Inspection) => void }) {
  const inspect = useInspectUpload();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [authority, setAuthority] = useState("");
  const [progress, setProgress] = useState(0);
  const [error, setError] = useState<{ message: string; link?: string } | null>(null);
  const [dragging, setDragging] = useState(false);

  const choose = (chosen: File | undefined) => {
    setError(null);
    if (!chosen) return;
    if (!/\.xlsx$/i.test(chosen.name)) return setError({ message: "Choose an .xlsx workbook." });
    if (chosen.size > MAX_BYTES) return setError({ message: `The file is ${(chosen.size / 1048576).toFixed(1)} MB; the limit is 25 MB.` });
    setFile(chosen);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!file) return;
    setError(null);
    setProgress(0);
    try {
      onInspected(await inspect.mutateAsync({ file, sourceAuthority: authority.trim() || undefined, onProgress: setProgress }));
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "duplicate_file") {
        setError({ message: "This exact file was uploaded before, byte for byte. Nothing new to ingest.", link: "/sanctions?tab=files" });
      } else setError({ message: cause instanceof Error ? cause.message : String(cause) });
    }
  };

  return (
    <Card>
      <CardBody className="pt-4">
        <form onSubmit={submit} className="space-y-4">
          <div
            className={cn(
              "flex flex-col items-center gap-2 rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors",
              dragging ? "border-accent bg-accent-wash/30" : "border-border-strong",
            )}
            onDragOver={(e: DragEvent) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e: DragEvent) => {
              e.preventDefault();
              setDragging(false);
              choose(e.dataTransfer.files[0]);
            }}
          >
            <FileSpreadsheet className="size-8 text-muted" aria-hidden />
            {file ? (
              <p className="text-sm">
                <span className="font-medium text-ink">{file.name}</span> <span className="text-muted">· {(file.size / 1024).toFixed(0)} KB</span>
              </p>
            ) : (
              <p className="text-sm text-muted">Drop an .xlsx exclusion list here, or</p>
            )}
            <Button onClick={() => input.current?.click()}>{file ? "Choose another" : "Choose a file"}</Button>
            <input ref={input} type="file" accept=".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" className="sr-only" aria-label="Sanction workbook" onChange={(e) => choose(e.target.files?.[0])} />
          </div>
          <div className="max-w-sm space-y-1">
            <Label htmlFor="authority">Source authority (optional)</Label>
            <Input id="authority" value={authority} onChange={(e) => setAuthority(e.target.value)} placeholder="e.g. OIG-LEIE" />
            <p className="text-xs text-muted">Who published the list. A saved mapping for this source is reused; a known layout is recognised even without it.</p>
          </div>
          {inspect.isPending ? (
            <div aria-live="polite">
              <div className="mb-1 flex justify-between text-xs text-muted">
                <span>{progress < 1 ? "Uploading" : "Inspecting columns"}</span>
                <span className="tabular">{Math.round(progress * 100)}%</span>
              </div>
              <div className="h-2 overflow-hidden rounded-full bg-accent-wash" role="progressbar" aria-valuenow={Math.round(progress * 100)} aria-valuemin={0} aria-valuemax={100}>
                <div className="h-full bg-series-1 transition-[width]" style={{ width: `${progress * 100}%` }} />
              </div>
            </div>
          ) : null}
          {error ? (
            <p role="alert" className="flex items-center gap-2 text-sm text-critical-text">
              <CircleAlert className="size-4 shrink-0" /> {error.message}{" "}
              {error.link ? (
                <Link to={error.link} className="text-accent hover:underline">
                  See the file history
                </Link>
              ) : null}
            </p>
          ) : null}
          <Button type="submit" variant="primary" disabled={!file} loading={inspect.isPending}>
            <UploadIcon /> Upload and inspect
          </Button>
        </form>
      </CardBody>
    </Card>
  );
}

// --- step 2: the column mapping ------------------------------------------------------

/**
 * Detected columns on the left, the canonical field each feeds on the right,
 * pre-filled with the proposal. Below, the sample rows re-rendered through the
 * mapping as it stands, so the effect of every change is visible before
 * anything is ingested.
 */
function MapStep({ inspection, onCommitted, onRestart }: { inspection: Inspection; onCommitted: (r: CommitResult) => void; onRestart: () => void }) {
  const commit = useCommitUpload(inspection.file_id);
  // column header -> canonical field, the direction the analyst edits in.
  const [byColumn, setByColumn] = useState<Record<string, string>>(() =>
    Object.fromEntries(Object.entries(inspection.proposed_mapping).map(([field, column]) => [column, field])),
  );
  const [authority, setAuthority] = useState(inspection.source_authority ?? "");
  const [saveDefault, setSaveDefault] = useState(inspection.proposal_source !== "stored");
  const [mappingName, setMappingName] = useState("");
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  const mapping = useMemo(() => Object.fromEntries(Object.entries(byColumn).filter(([, f]) => f).map(([column, field]) => [field, column])), [byColumn]);
  const hasName = NAME_FIELDS.some((f) => mapping[f]);
  const mappedFields = Object.keys(mapping);

  const assign = (column: string, field: string) => {
    setServerErrors({});
    setByColumn((current) => {
      const next = { ...current };
      // A canonical field reads from one column: taking it here frees it elsewhere.
      if (field) for (const [c, f] of Object.entries(next)) if (f === field) next[c] = "";
      next[column] = field;
      return next;
    });
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError(null);
    setServerErrors({});
    try {
      onCommitted(
        await commit.mutateAsync({
          mapping,
          source_authority: authority.trim() || undefined,
          save_as_default: saveDefault,
          mapping_name: mappingName.trim() || undefined,
        }),
      );
    } catch (cause) {
      if (cause instanceof ApiError && cause.code === "invalid_mapping") {
        setServerErrors(Object.fromEntries(Object.entries(cause.details).map(([k, v]) => [k.replace(/^mapping\./, ""), String(v)])));
        setError("The mapping was refused. Each problem is marked beside its field.");
      } else setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  const sampleCount = Math.min(5, Math.max(0, ...inspection.columns.map((c) => c.samples.length)));
  const columnOf = (field: string) => inspection.columns.find((c) => c.name === mapping[field]);

  return (
    <form onSubmit={submit} className="space-y-4">
      <Card>
        <CardHeader
          title={
            <span>
              {inspection.filename} <span className="font-normal text-muted">· {formatInt(inspection.rows)} data rows · {inspection.columns.length} columns</span>
            </span>
          }
          description={
            inspection.proposal_source === "stored"
              ? "Pre-filled from the saved mapping for this source. Confirm or adjust."
              : "Pre-filled by matching the headers to canonical fields. Check every row."
          }
          actions={
            <Button size="sm" variant="ghost" onClick={onRestart}>
              Choose another file
            </Button>
          }
        />
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <caption className="sr-only">Map each detected column to a canonical field</caption>
            <thead className="bg-surface-2 text-left text-xs text-muted">
              <tr>
                <th scope="col" className="px-4 py-2 font-medium">Detected column</th>
                <th scope="col" className="px-3 py-2 font-medium">Sample values</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Filled</th>
                <th scope="col" className="w-64 px-4 py-2 font-medium">Canonical field</th>
              </tr>
            </thead>
            <tbody>
              {inspection.columns.map((column) => {
                const field = byColumn[column.name] ?? "";
                const problem = field ? serverErrors[field] : undefined;
                return (
                  <tr key={column.name} className="border-t">
                    <th scope="row" className="px-4 py-2 text-left font-mono text-xs font-medium text-ink">
                      {column.name}
                    </th>
                    <td className="max-w-80 truncate px-3 py-2 text-xs text-ink-2" title={column.samples.join(" · ")}>
                      {column.samples.slice(0, 3).join(" · ") || <span className="text-muted">empty</span>}
                    </td>
                    <td className="tabular px-3 py-2 text-right text-xs text-muted">{formatInt(column.non_empty)}</td>
                    <td className="px-4 py-2">
                      <Select
                        aria-label={`Canonical field for ${column.name}`}
                        value={field}
                        onChange={(e) => assign(column.name, e.target.value)}
                        aria-invalid={Boolean(problem)}
                      >
                        <option value="">— not imported (kept in raw) —</option>
                        {inspection.canonical_fields.map((f) => (
                          <option key={f} value={f}>
                            {humanize(f)}
                            {NAME_FIELDS.includes(f) ? " *" : ""}
                          </option>
                        ))}
                      </Select>
                      <FieldError>{problem ?? null}</FieldError>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <CardBody className="pt-3">
          {!hasName ? (
            <p role="alert" className="flex items-center gap-2 text-sm text-critical-text">
              <CircleAlert className="size-4" /> Map a column to <strong>Last name</strong> or <strong>Organization name</strong>. Every record needs a name to be matched on.
            </p>
          ) : (
            <p className="text-xs text-muted">* one of these is required. Unmapped columns are not lost: every row is kept verbatim with the record.</p>
          )}
          {Object.entries(serverErrors)
            .filter(([f]) => !Object.values(byColumn).includes(f))
            .map(([f, message]) => (
              <p key={f} className="text-sm text-critical-text" role="alert">
                {humanize(f)}: {message}
              </p>
            ))}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Preview" description="The first sample rows, read through the mapping above. This is what will be ingested." />
        <div className="overflow-x-auto">
          {mappedFields.length === 0 ? (
            <p className="px-4 pb-4 text-sm text-muted">Map a column to see the preview.</p>
          ) : (
            <table className="w-full text-xs">
              <thead className="bg-surface-2 text-left text-muted">
                <tr>
                  {mappedFields.map((field) => (
                    <th key={field} scope="col" className="px-3 py-2 font-medium whitespace-nowrap">
                      {humanize(field)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {Array.from({ length: sampleCount }, (_, i) => (
                  <tr key={i} className="border-t">
                    {mappedFields.map((field) => (
                      <td key={field} className="px-3 py-1.5 whitespace-nowrap text-ink">
                        {columnOf(field)?.samples[i] ?? <span className="text-muted">—</span>}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
        <p className="px-4 py-2 text-[11px] text-muted">Samples are the first non-empty values of each column, so a row here can mix source rows.</p>
      </Card>

      <Card>
        <CardBody className="grid gap-4 pt-4 md:grid-cols-3">
          <div className="space-y-1">
            <Label htmlFor="commit-authority">Source authority</Label>
            <Input id="commit-authority" value={authority} onChange={(e) => setAuthority(e.target.value)} placeholder="e.g. OIG-LEIE" />
          </div>
          <div className="space-y-1">
            <Label htmlFor="mapping-name">Mapping name (optional)</Label>
            <Input id="mapping-name" value={mappingName} onChange={(e) => setMappingName(e.target.value)} placeholder="e.g. LEIE monthly" />
          </div>
          <label className="flex items-center gap-2 self-end pb-1.5 text-sm text-ink">
            <input type="checkbox" className="size-4 accent-[var(--accent)]" checked={saveDefault} onChange={(e) => setSaveDefault(e.target.checked)} />
            Save as the default for this source
          </label>
        </CardBody>
        <div className="flex items-center justify-between gap-3 border-t px-4 py-3">
          {error ? (
            <p role="alert" className="text-sm text-critical-text">
              {error}
            </p>
          ) : (
            <span className="text-xs text-muted">Committing parses every row. A changed record becomes a new version; the old one is kept for replay.</span>
          )}
          <Button type="submit" variant="primary" disabled={!hasName} loading={commit.isPending}>
            Commit {formatInt(inspection.rows)} rows
          </Button>
        </div>
      </Card>
    </form>
  );
}

// --- step 3: result, then reconcile ------------------------------------------------------

function DoneStep({ committed, fileId, runId }: { committed: CommitResult | null; fileId: string | null; runId: string | null }) {
  const start = useStartRun();
  const navigate = useNavigate();
  const [started, setStarted] = useState<string | null>(runId);
  const [error, setError] = useState<string | null>(null);
  const runs = useRuns(fileId ?? undefined);
  const run = runs.data?.items.find((r) => r.id === started) ?? null;

  const reconcile = async () => {
    if (!fileId) return;
    setError(null);
    try {
      const queued = await start.mutateAsync({ file_id: fileId });
      setStarted(queued.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  };

  return (
    <div className="space-y-4">
      {committed ? (
        <Card>
          <CardHeader title="Ingested" description={`Mapping saved and recorded against the file, so its interpretation is replayable.`} />
          <CardBody className="space-y-3">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
              {(
                [
                  ["New records", committed.new],
                  ["New versions", committed.replaced],
                  ["Unchanged", committed.unchanged],
                  ["Warnings", committed.warnings],
                  ["Rejected rows", committed.rejected],
                ] as const
              ).map(([label, value]) => (
                <div key={label} className="rounded-md bg-surface-2 px-3 py-2">
                  <p className="text-xs text-muted">{label}</p>
                  <p className="text-xl font-semibold text-ink">{formatInt(value)}</p>
                </div>
              ))}
            </div>
            {committed.rejected_rows.length > 0 ? (
              <details className="text-sm" open={committed.rejected_rows.length <= 10}>
                <summary className="cursor-pointer text-critical-text">{committed.rejected_rows.length} rows could not be read</summary>
                <ul className="mt-2 space-y-1 text-xs">
                  {committed.rejected_rows.slice(0, 50).map((issue) => (
                    <li key={`${issue.row}-${issue.field ?? ""}`}>
                      Row <span className="tabular font-medium">{issue.row}</span>
                      {issue.field ? <> · {humanize(issue.field)}</> : null}: {issue.reason}
                    </li>
                  ))}
                </ul>
              </details>
            ) : null}
          </CardBody>
        </Card>
      ) : null}

      <Card>
        <CardHeader title="Reconcile this file" description="Matches its current records against the provider master. Runs in the background; results land in the review queue." />
        <CardBody className="space-y-3">
          {!started ? (
            <Button variant="primary" onClick={reconcile} loading={start.isPending} disabled={!fileId}>
              <Play /> Start reconciliation
            </Button>
          ) : run ? (
            <RunProgress run={run} />
          ) : (
            <p className="text-sm text-muted">Waiting for the run…</p>
          )}
          {error ? (
            <p role="alert" className="text-sm text-critical-text">
              {error}
            </p>
          ) : null}
          {run?.status === "COMPLETED" ? (
            <Button variant="primary" onClick={() => navigate(`/queue?review_status=&run_id=${run.id}`)}>
              Review the results <ArrowRight />
            </Button>
          ) : null}
        </CardBody>
      </Card>
    </div>
  );
}

function RunProgress({ run }: { run: Run }) {
  const decided = run.matched_count + run.ambiguous_count + run.no_match_count;
  const fraction = run.records_total > 0 ? decided / run.records_total : run.status === "COMPLETED" ? 1 : 0;
  return (
    <div className="space-y-2" aria-live="polite">
      <div className="flex items-center gap-3">
        <StatusBadge status={run.status} />
        <span className="font-mono text-xs text-muted">run {run.id.slice(0, 8)}</span>
        {run.scoring_config_version ? (
          <span className="font-mono text-xs text-muted">config {run.scoring_config_version}</span>
        ) : null}
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-accent-wash">
        <div className="h-full bg-series-1 transition-[width]" style={{ width: `${fraction * 100}%` }} />
      </div>
      <p className="tabular text-xs text-ink-2">
        {formatInt(decided)} of {formatInt(run.records_total)} records · {formatInt(run.matched_count)} matched · {formatInt(run.ambiguous_count)} ambiguous ·{" "}
        {formatInt(run.no_match_count)} unmatched · LLM {formatInt(run.llm_calls)} calls, {formatUsd(run.llm_cost_usd)}
      </p>
      {run.status === "QUEUED" ? <p className="text-xs text-muted">Queued. A worker picks it up (`make worker`).</p> : null}
      {run.error ? <p className="text-xs text-critical-text">{run.error}</p> : null}
    </div>
  );
}
