import { AlertTriangle, Database, Send, Sparkles } from "lucide-react";
import { type FormEvent, useState } from "react";
import { Bar, BarChart, CartesianGrid, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { useAskAssistant, useAssistantHistory, useAssistantSchema } from "@/api/queries";
import type { AssistantAnswer } from "@/api/types";
import { ChartTooltip, useChartTheme } from "@/components/ChartCard";
import { EmptyState, ErrorState, PageHeader } from "@/components/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/form";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui/surface";
import { formatDateTime, formatInt } from "@/lib/format";

const EXAMPLES = [
  "how many records are waiting for review?",
  "which states have the most unmatched sanction records?",
  "what did the engine decide, broken down by decision?",
  "how many cases are active?",
];

/** A two-column answer whose second column is a number reads as a bar chart. */
function chartable(answer: AssistantAnswer): { label: string; value: number }[] | null {
  if (answer.columns.length !== 2 || answer.rows.length < 2 || answer.rows.length > 25) return null;
  const rows = answer.rows.map((row) => ({ label: String(row[0] ?? "—"), value: Number(row[1]) }));
  return rows.every((r) => Number.isFinite(r.value)) ? rows : null;
}

function AnswerChart({ rows, measure }: { rows: { label: string; value: number }[]; measure: string }) {
  const t = useChartTheme();
  return (
    <figure role="img" aria-label={`Bar chart of ${measure} by category`} style={{ height: Math.max(180, rows.length * 28) }}>
      <ResponsiveContainer>
        <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 48, bottom: 4, left: 8 }}>
          <CartesianGrid stroke={t.grid} horizontal={false} />
          <XAxis type="number" tick={{ fontSize: 11, fill: t.muted }} tickLine={false} axisLine={{ stroke: t.axis }} />
          <YAxis
            type="category"
            dataKey="label"
            width={140}
            tick={{ fontSize: 11, fill: t.muted }}
            tickLine={false}
            axisLine={false}
          />
          <Tooltip
            cursor={{ fill: t.hover }}
            content={({ active, payload }) => (
              <ChartTooltip
                active={active}
                label={String(payload?.[0]?.payload?.label ?? "")}
                items={[{ name: measure, value: formatInt(Number(payload?.[0]?.value)), color: t.series1 }]}
              />
            )}
          />
          <Bar dataKey="value" fill={t.series1} radius={[0, 4, 4, 0]} isAnimationActive={false} barSize={14}>
            <LabelList dataKey="value" position="right" fill={t.ink2} fontSize={11} formatter={(v: number) => formatInt(v)} />
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </figure>
  );
}

function AnswerTable({ answer }: { answer: AssistantAnswer }) {
  return (
    <div className="overflow-auto" style={{ maxHeight: 420 }}>
      <table className="w-full text-sm">
        <thead className="sticky top-0 bg-surface text-left text-xs text-muted">
          <tr>
            {answer.columns.map((c) => (
              <th key={c} scope="col" className="px-3 py-2 font-medium whitespace-nowrap">
                {c}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {answer.rows.map((row, i) => (
            <tr key={i} className="border-t">
              {row.map((cell, j) => (
                <td key={j} className={typeof cell === "number" ? "tabular px-3 py-1.5 text-right" : "px-3 py-1.5"}>
                  {cell === null ? <span className="text-muted">null</span> : String(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The SQL that ran, always shown: an answer nobody can check is not an answer. */
function Sql({ sql, refused }: { sql: string; refused: boolean }) {
  return (
    <pre
      className={`overflow-x-auto rounded-md px-3 py-2 font-mono text-xs ring-1 ${
        refused ? "bg-critical/10 text-ink-2 ring-critical/30" : "bg-surface-2 text-ink-2 ring-border"
      }`}
    >
      {sql}
    </pre>
  );
}

export function AssistantPage() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AssistantAnswer | null>(null);
  const ask = useAskAssistant();
  const history = useAssistantHistory();
  const schema = useAssistantSchema();

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const asked = question.trim();
    if (!asked) return;
    ask.mutate({ question: asked }, { onSuccess: (out) => setAnswer(out) });
  };
  const run = (asked: string) => {
    setQuestion(asked);
    ask.mutate({ question: asked }, { onSuccess: (out) => setAnswer(out) });
  };

  const bars = answer && answer.rejected == null ? chartable(answer) : null;

  return (
    <>
      <PageHeader
        title="Assistant"
        description="Ask about the data in English. It writes one read-only SELECT over five views, shows you the SQL, and runs it as a role that can read nothing else."
      />

      <Card className="mb-4">
        <CardBody className="py-3">
          <form onSubmit={submit} className="flex flex-wrap items-center gap-2">
            <label htmlFor="question" className="sr-only">
              Your question
            </label>
            <Input
              id="question"
              className="min-w-64 flex-1"
              placeholder="how many records are waiting for review?"
              value={question}
              maxLength={500}
              onChange={(e) => setQuestion(e.target.value)}
              autoComplete="off"
            />
            <Button type="submit" variant="primary" loading={ask.isPending} disabled={!question.trim()}>
              <Send /> Ask
            </Button>
          </form>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {EXAMPLES.map((example) => (
              <button
                key={example}
                type="button"
                onClick={() => run(example)}
                className="rounded-full px-2.5 py-1 text-xs text-ink-2 ring-1 ring-border transition-colors hover:text-ink hover:ring-border-strong"
              >
                {example}
              </button>
            ))}
          </div>
        </CardBody>
      </Card>

      {ask.error ? (
        <Card className="mb-4">
          <ErrorState error={ask.error} />
        </Card>
      ) : null}

      <div className="grid gap-4 xl:grid-cols-3">
        <div className="space-y-4 xl:col-span-2">
          {ask.isPending ? (
            <Card>
              <CardBody>
                <Skeleton className="h-40" />
              </CardBody>
            </Card>
          ) : answer ? (
            <Card>
              <CardHeader
                title={answer.rejected ? "Refused" : `${formatInt(answer.row_count)} row${answer.row_count === 1 ? "" : "s"}`}
                description={
                  answer.rejected
                    ? "The query was not run. What it tried is below."
                    : `${answer.seconds.toFixed(2)}s · ${answer.model ?? "no model"} · prompt ${answer.prompt_version}`
                }
              />
              <CardBody className="space-y-3">
                {answer.rejected ? (
                  <p className="flex items-start gap-2 text-sm text-ink">
                    <AlertTriangle className="mt-0.5 size-4 shrink-0 text-critical-text" aria-hidden />
                    <span>
                      {answer.rejected}
                      {answer.rejection_code ? <span className="ml-1 text-xs text-muted">({answer.rejection_code})</span> : null}
                    </span>
                  </p>
                ) : null}
                {answer.sql ? <Sql sql={answer.sql} refused={answer.rejected != null} /> : null}
                {answer.notes.map((note) => (
                  <p key={note} className="text-xs text-muted">
                    {note}
                  </p>
                ))}
                {!answer.rejected && answer.rows.length > 0 ? (
                  <>
                    {bars ? <AnswerChart rows={bars} measure={answer.columns[1] ?? "value"} /> : null}
                    <AnswerTable answer={answer} />
                  </>
                ) : null}
                {!answer.rejected && answer.rows.length === 0 ? (
                  <p className="text-sm text-muted">No rows matched.</p>
                ) : null}
              </CardBody>
            </Card>
          ) : (
            <Card>
              <EmptyState
                title="Ask a question"
                body="It only ever reads. Every question and the SQL it produced are written to the audit log."
              />
            </Card>
          )}
        </div>

        <div className="space-y-4">
          <Card>
            <CardHeader title="Recent questions" description="Yours, newest first." />
            <CardBody>
              {history.isLoading ? (
                <Skeleton className="h-24" />
              ) : history.data?.length ? (
                <ol className="space-y-2">
                  {history.data.map((row, i) => (
                    <li key={`${row.asked_at}-${i}`}>
                      <button
                        type="button"
                        onClick={() => run(row.question)}
                        className="text-left text-sm text-ink-2 underline decoration-border underline-offset-2 hover:text-ink"
                      >
                        {row.question}
                      </button>
                      <p className="text-xs text-muted">
                        {formatDateTime(row.asked_at)} · {row.rejected ? "refused" : `${formatInt(row.rows)} rows`}
                      </p>
                    </li>
                  ))}
                </ol>
              ) : (
                <p className="text-sm text-muted">Nothing asked yet.</p>
              )}
            </CardBody>
          </Card>

          <Card>
            <CardHeader
              title={
                <span className="inline-flex items-center gap-2">
                  <Database className="size-4 text-muted" aria-hidden /> What it can read
                </span>
              }
              description="Five views. Not the user table, not the audit log, not the raw model calls."
            />
            <CardBody>
              {schema.data ? (
                <dl className="space-y-2 text-xs">
                  {schema.data.views.map((view) => (
                    <div key={view.name}>
                      <dt className="font-mono text-ink">{view.name}</dt>
                      <dd className="text-muted">{view.about}</dd>
                    </div>
                  ))}
                </dl>
              ) : (
                <Skeleton className="h-24" />
              )}
            </CardBody>
          </Card>
        </div>
      </div>

      <p className="mt-4 flex items-start gap-1.5 text-xs text-muted">
        <Sparkles className="mt-px size-3.5 shrink-0" aria-hidden />
        <span>
          A model writes the SQL, so nothing it writes is trusted: the query is parsed and refused unless it is a single
          bounded SELECT over those views, and it runs as a role granted nothing else. Read the SQL before you act on the
          answer.
        </span>
      </p>
    </>
  );
}
