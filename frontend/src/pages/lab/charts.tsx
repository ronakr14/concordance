import {
  CartesianGrid,
  ComposedChart,
  ErrorBar,
  Legend,
  Line,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";

import type { LabCalibration } from "@/api/types";
import { ChartCard, ChartTooltip, MiniTable, useChartTheme } from "@/components/ChartCard";
import { formatInt, formatPct } from "@/lib/format";

import {
  type CurveRow,
  type LlmPoint,
  type Metric,
  METRICS,
  pct,
  STRATEGIES,
  type StrategyKey,
} from "./model";

const TICK = { fontSize: 11 };

/**
 * The robustness curve: one line per strategy across corruption levels, and
 * the LLM-assisted engine as sampled points with their 95% interval. Points,
 * not a line, because it was measured at three levels on a sample and joining
 * them would claim a curve nobody measured.
 */
export function RobustnessChart({
  rows,
  llm,
  metric,
  visible,
  level,
  onPick,
  loading,
  error,
  onRetry,
}: {
  rows: CurveRow[];
  llm: LlmPoint[];
  metric: Metric;
  visible: Set<StrategyKey>;
  level: number;
  onPick: (level: number) => void;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const t = useChartTheme();
  const metricLabel = METRICS.find((m) => m.key === metric)?.label ?? metric;
  const lines = STRATEGIES.filter((s) => s.key !== "probabilistic_llm" && visible.has(s.key));
  const showLlm = visible.has("probabilistic_llm") && llm.length > 0;
  const llmSpec = STRATEGIES.find((s) => s.key === "probabilistic_llm")!;
  const last = rows.length - 1;

  return (
    <ChartCard
      title="Robustness curve"
      description={`${metricLabel} against ground truth as corruption rises. Click a level to inspect it below.`}
      label={`Line chart of ${metricLabel} by corruption level for each strategy`}
      loading={loading}
      error={error}
      onRetry={onRetry}
      empty={rows.length === 0}
      height={340}
      table={
        <MiniTable
          head={["Corruption", ...lines.map((s) => s.label), ...(showLlm ? ["Probabilistic + LLM (95% CI)"] : [])]}
          rows={rows.map((row) => {
            const point = llm.find((p) => p.level === row.level);
            return [
              pct(row.level),
              ...lines.map((s) => formatPct(row[s.key])),
              ...(showLlm ? [point ? `${formatPct(point.value)} (${formatPct(point.lo)}–${formatPct(point.hi)})` : "—"] : []),
            ];
          })}
        />
      }
      chart={
        <ResponsiveContainer>
          <ComposedChart
            data={rows}
            margin={{ top: 8, right: 112, bottom: 4, left: 0 }}
            onClick={(state) => {
              // A numeric axis reports the hovered row by index, not always by label.
              const s = state as { activeLabel?: unknown; activeTooltipIndex?: unknown } | null;
              const index = Number(s?.activeTooltipIndex);
              const picked = Number.isInteger(index) && rows[index] ? rows[index].level : Number(s?.activeLabel);
              if (Number.isFinite(picked)) onPick(picked);
            }}
            style={{ cursor: "pointer" }}
          >
            <CartesianGrid stroke={t.grid} vertical={false} />
            <XAxis
              type="number"
              dataKey="level"
              domain={[0, 0.9]}
              ticks={rows.map((r) => r.level)}
              tickFormatter={pct}
              tick={{ ...TICK, fill: t.muted }}
              tickLine={false}
              axisLine={{ stroke: t.axis }}
              label={{ value: "Corruption", position: "insideBottomRight", offset: -2, fill: t.muted, fontSize: 11 }}
              height={32}
            />
            <YAxis
              domain={[0, 1]}
              ticks={[0, 0.25, 0.5, 0.75, 1]}
              tickFormatter={(v: number) => formatPct(v, 0)}
              tick={{ ...TICK, fill: t.muted }}
              tickLine={false}
              axisLine={false}
              width={44}
            />
            <ReferenceLine
              x={level}
              stroke={t.axis}
              strokeWidth={2}
              label={{ value: pct(level), position: "top", fill: t.ink2, fontSize: 11 }}
            />
            <Tooltip
              cursor={{ stroke: t.axis }}
              content={({ active, label }) => {
                const row = rows.find((r) => r.level === Number(label));
                if (!row) return null;
                const point = llm.find((p) => p.level === row.level);
                return (
                  <ChartTooltip
                    active={active}
                    label={`${pct(row.level)} corruption · ${metricLabel}`}
                    items={[
                      ...lines.map((s) => ({ name: s.label, value: formatPct(row[s.key]), color: t[s.color] })),
                      ...(showLlm && point
                        ? [{ name: `${llmSpec.label} (${formatPct(point.lo)}–${formatPct(point.hi)})`, value: formatPct(point.value), color: t[llmSpec.color] }]
                        : []),
                    ]}
                  />
                );
              }}
            />
            <Legend verticalAlign="top" align="left" height={28} iconType="plainline" wrapperStyle={{ fontSize: 12 }} formatter={(value) => <span style={{ color: t.ink2 }}>{value}</span>} />
            {lines.map((s) => (
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
            {showLlm ? (
              <Scatter
                data={llm}
                dataKey="value"
                name={`${llmSpec.label} (sampled)`}
                fill={t[llmSpec.color]}
                shape={(props: { cx?: number; cy?: number }) => (
                  <path
                    d={`M${props.cx},${(props.cy ?? 0) - 6} L${(props.cx ?? 0) + 6},${props.cy} L${props.cx},${(props.cy ?? 0) + 6} L${(props.cx ?? 0) - 6},${props.cy} Z`}
                    fill={t[llmSpec.color]}
                    stroke={t.surface}
                    strokeWidth={2}
                  />
                )}
                isAnimationActive={false}
              >
                <ErrorBar dataKey="err" direction="y" width={8} stroke={t[llmSpec.color]} strokeWidth={2} />
              </Scatter>
            ) : null}
          </ComposedChart>
        </ResponsiveContainer>
      }
    />
  );
}

type BinPoint = { x: number; y: number; count: number };

const points = (bins: LabCalibration["before"]["bins"]): BinPoint[] =>
  bins.filter((b) => b.count > 0).map((b) => ({ x: b.mean_predicted, y: b.observed_frequency, count: b.count }));

/**
 * The reliability diagram, before and after isotonic calibration, on the fit's
 * holdout. A point on the diagonal means "when the engine said 80%, it was
 * right 80% of the time". Each bin is a point sized by how many records it
 * holds, not a line through the bins: most records sit near 0 or 1, the middle
 * bins hold a handful each, and a line would draw their noise as a shape. The
 * uncalibrated posterior is in neutral ink because it is the reference the
 * calibrated points are compared against, not a peer series.
 */
export function ReliabilityChart({
  entry,
  level,
  model,
  loading,
  error,
  onRetry,
}: {
  entry: LabCalibration | undefined;
  level: number;
  model: string;
  loading: boolean;
  error: unknown;
  onRetry: () => void;
}) {
  const t = useChartTheme();
  const before = entry ? points(entry.before.bins) : [];
  const after = entry ? points(entry.after.bins) : [];
  const beforeName = `Before calibration · ECE ${entry?.before.ece?.toFixed(3) ?? "—"}`;
  const afterName = `After isotonic · ECE ${entry?.after.ece?.toFixed(3) ?? "—"}`;
  const largest = Math.max(1, ...before.map((b) => b.count), ...after.map((b) => b.count));
  const kind = model === "organization" ? "organization" : "individual";
  const holdout = entry?.n_holdout ? ` (${formatInt(entry.n_holdout)} records)` : "";

  return (
    <ChartCard
      title="Reliability diagram"
      description={`Observed match rate against predicted confidence, ${kind} model at ${pct(level)} corruption, on the fit's holdout${holdout}. The diagonal is perfect calibration; point size is records per bin.`}
      label="Scatter chart of observed match rate against predicted confidence, before and after calibration, with the perfect-calibration diagonal"
      loading={loading}
      error={error}
      onRetry={onRetry}
      empty={!entry || (before.length === 0 && after.length === 0)}
      height={320}
      table={
        <MiniTable
          head={["Bin", "Before: predicted", "Before: observed", "After: predicted", "After: observed", "Records"]}
          rows={(entry?.after.bins ?? []).map((bin, i) => {
            const b = entry?.before.bins[i];
            return [
              `${formatPct(bin.lower, 0)}–${formatPct(bin.upper, 0)}`,
              b && b.count ? formatPct(b.mean_predicted) : "—",
              b && b.count ? formatPct(b.observed_frequency) : "—",
              bin.count ? formatPct(bin.mean_predicted) : "—",
              bin.count ? formatPct(bin.observed_frequency) : "—",
              formatInt(bin.count),
            ];
          })}
        />
      }
      chart={
        <ResponsiveContainer>
          <ComposedChart margin={{ top: 12, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke={t.grid} />
            <XAxis
              type="number"
              dataKey="x"
              domain={[0, 1]}
              ticks={[0, 0.2, 0.4, 0.6, 0.8, 1]}
              tickFormatter={(v: number) => formatPct(v, 0)}
              tick={{ ...TICK, fill: t.muted }}
              tickLine={false}
              axisLine={{ stroke: t.axis }}
              label={{ value: "Predicted confidence", position: "insideBottomRight", offset: -2, fill: t.muted, fontSize: 11 }}
              height={32}
            />
            <YAxis
              type="number"
              dataKey="y"
              domain={[0, 1]}
              ticks={[0, 0.2, 0.4, 0.6, 0.8, 1]}
              tickFormatter={(v: number) => formatPct(v, 0)}
              tick={{ ...TICK, fill: t.muted }}
              tickLine={false}
              axisLine={false}
              width={44}
            />
            <ZAxis type="number" dataKey="count" domain={[1, largest]} range={[40, 520]} />
            <ReferenceLine
              segment={[
                { x: 0, y: 0 },
                { x: 1, y: 1 },
              ]}
              stroke={t.axis}
              strokeWidth={1.5}
            />
            <Tooltip
              cursor={false}
              content={({ active, payload }) => {
                const p = payload?.[0];
                const bin = p?.payload as BinPoint | undefined;
                if (!bin) return null;
                const color = String(p?.color ?? t.series1);
                return (
                  <ChartTooltip
                    active={active}
                    label={String(p?.name ?? "")}
                    items={[
                      { name: "Predicted", value: formatPct(bin.x), color },
                      { name: "Observed", value: formatPct(bin.y), color },
                      { name: "Records", value: formatInt(bin.count), color },
                    ]}
                  />
                );
              }}
            />
            <Legend verticalAlign="bottom" align="left" height={24} iconType="circle" wrapperStyle={{ fontSize: 12, paddingTop: 4 }} formatter={(value) => <span style={{ color: t.ink2 }}>{value}</span>} />
            <Scatter data={before} name={beforeName} fill={t.muted} fillOpacity={0.55} stroke={t.surface} strokeWidth={1.5} isAnimationActive={false} />
            <Scatter data={after} name={afterName} fill={t.series1} fillOpacity={0.85} stroke={t.surface} strokeWidth={1.5} isAnimationActive={false} />
          </ComposedChart>
        </ResponsiveContainer>
      }
    />
  );
}
