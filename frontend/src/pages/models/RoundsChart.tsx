import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import type { FeedbackRound } from "@/api/types";
import { ChartCard, ChartTooltip, MiniTable, useChartTheme } from "@/components/ChartCard";
import { formatInt, formatPct } from "@/lib/format";

type Key = "precision" | "recall" | "f1";

/**
 * Fixed slots, the same order as the Lab's: precision and recall are the two
 * measures a reviewer trades, F1 is their summary, so it is dashed - it is a
 * derived line, not a third thing that was measured.
 */
const SERIES: { key: Key; label: string; color: "series1" | "series2" | "series3"; dash?: string }[] = [
  { key: "precision", label: "Precision", color: "series1" },
  { key: "recall", label: "Recall", color: "series2" },
  { key: "f1", label: "F1", color: "series3", dash: "6 4" },
];

type Row = { round: number; labels: number; review: number } & Record<Key, number | null>;

function rowsOf(rounds: FeedbackRound[]): Row[] {
  return rounds.map((r) => {
    const t = r.truth as Record<string, number | null>;
    return {
      round: r.round,
      labels: r.labels.total ?? 0,
      review: Number(t.review_share ?? 0),
      precision: t.precision ?? null,
      recall: t.recall ?? null,
      f1: t.f1 ?? null,
    };
  });
}

/**
 * Precision, recall and F1 after each simulated review round, judged against
 * ground truth on records no reviewer saw. Round 0 is the starting config.
 * The y-axis is zoomed to the data: these are lines, not bars, and the story
 * is movement of a few points, which a 0-100% axis flattens away.
 */
export function RoundsChart({
  rounds,
  loading,
  error,
  onRetry,
  actions,
}: {
  rounds: FeedbackRound[];
  loading: boolean;
  error: unknown;
  onRetry: () => void;
  actions?: React.ReactNode;
}) {
  const t = useChartTheme();
  const rows = rowsOf(rounds);
  const values = rows.flatMap((r) => SERIES.map((s) => r[s.key])).filter((v): v is number => v != null);
  const lo = values.length ? Math.max(0, Math.floor((Math.min(...values) - 0.02) * 20) / 20) : 0;
  const last = rows.length - 1;

  return (
    <ChartCard
      title="Precision and recall by review round"
      description="Each round a simulated reviewer labels the queue, the config is retuned on every label so far, and the result is judged against ground truth on records nobody reviewed."
      label="Line chart of precision, recall and F1 after each review round"
      loading={loading}
      error={error}
      onRetry={onRetry}
      empty={rows.length === 0}
      height={300}
      actions={actions}
      table={
        <MiniTable
          head={["Round", "Labels", "Precision", "Recall", "F1", "Grey band"]}
          rows={rows.map((r) => [
            r.round === 0 ? "Start" : String(r.round),
            formatInt(r.labels),
            formatPct(r.precision),
            formatPct(r.recall),
            formatPct(r.f1),
            formatPct(r.review),
          ])}
        />
      }
      chart={
        <ResponsiveContainer>
          <LineChart data={rows} margin={{ top: 8, right: 84, bottom: 4, left: 0 }}>
            <CartesianGrid stroke={t.grid} vertical={false} />
            <XAxis
              dataKey="round"
              type="number"
              domain={[0, Math.max(1, last)]}
              ticks={rows.map((r) => r.round)}
              tickFormatter={(v: number) => (v === 0 ? "Start" : `R${v}`)}
              tick={{ fontSize: 11, fill: t.muted }}
              tickLine={false}
              axisLine={{ stroke: t.axis }}
              height={28}
            />
            <YAxis
              domain={[lo, 1]}
              tickFormatter={(v: number) => formatPct(v, 0)}
              tick={{ fontSize: 11, fill: t.muted }}
              tickLine={false}
              axisLine={false}
              width={44}
            />
            <Tooltip
              cursor={{ stroke: t.axis }}
              content={({ active, label }) => {
                const row = rows.find((r) => r.round === Number(label));
                if (!row) return null;
                return (
                  <ChartTooltip
                    active={active}
                    label={`${row.round === 0 ? "Starting config" : `Round ${row.round}`} · ${formatInt(row.labels)} labels · grey band ${formatPct(row.review)}`}
                    items={SERIES.map((s) => ({ name: s.label, value: formatPct(row[s.key]), color: t[s.color] }))}
                  />
                );
              }}
            />
            <Legend verticalAlign="top" align="left" height={28} iconType="plainline" wrapperStyle={{ fontSize: 12 }} formatter={(value) => <span style={{ color: t.ink2 }}>{value}</span>} />
            {SERIES.map((s) => (
              <Line
                key={s.key}
                type="linear"
                dataKey={s.key}
                name={s.label}
                stroke={t[s.color]}
                strokeWidth={2}
                strokeDasharray={s.dash}
                dot={{ r: 4, fill: t[s.color], stroke: t.surface, strokeWidth: 2 }}
                activeDot={{ r: 5, stroke: t.surface, strokeWidth: 2 }}
                isAnimationActive={false}
                label={(props: { x?: number | string; y?: number | string; index?: number }) =>
                  props.index === last ? (
                    <text key={s.key} x={Number(props.x) + 10} y={Number(props.y) + 4} fill={t.ink2} fontSize={11}>
                      {s.label}
                    </text>
                  ) : (
                    <g key={`${s.key}-${props.index}`} />
                  )
                }
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      }
    />
  );
}
