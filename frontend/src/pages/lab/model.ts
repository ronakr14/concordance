// What the Lab draws, derived from `/lab/results` in one place so the charts,
// the tables beside them and the summary tiles can never disagree.

import type { LabCalibration, LabCell, LabLlmLevel, LabResults } from "@/api/types";

export type Metric = "f1" | "precision" | "recall";
export type StrategyKey = "deterministic" | "fuzzy" | "probabilistic" | "probabilistic_llm";
export type SeriesToken = "series1" | "series2" | "series3" | "series4";

/**
 * Fixed slots, never reassigned: a strategy keeps its colour whichever others
 * are toggled off. The engine takes slot 1 because it is the line the page is
 * about; the LLM-assisted variant sits next to it in slot 2. The two baselines
 * are also dashed, so identity never rests on hue alone.
 */
export const STRATEGIES: { key: StrategyKey; label: string; color: SeriesToken; dash?: string }[] = [
  { key: "probabilistic", label: "Probabilistic", color: "series1" },
  { key: "probabilistic_llm", label: "Probabilistic + LLM", color: "series2" },
  { key: "fuzzy", label: "Fuzzy", color: "series4", dash: "6 4" },
  { key: "deterministic", label: "Deterministic", color: "series3", dash: "2 4" },
];

export const METRICS: { key: Metric; label: string }[] = [
  { key: "f1", label: "F1" },
  { key: "precision", label: "Precision" },
  { key: "recall", label: "Recall" },
];

export const strategyLabel = (key: string) => STRATEGIES.find((s) => s.key === key)?.label ?? key;

export const pct = (level: number) => `${Math.round(level * 100)}%`;

/** Levels the sweep measured, ascending. */
export function levelsOf(results: LabResults | undefined): number[] {
  const set = new Set<number>();
  for (const cell of results?.cells ?? []) if (cell.level != null) set.add(cell.level);
  return [...set].sort((a, b) => a - b);
}

/** The measured level nearest the one asked for. */
export function nearest(levels: number[], wanted: number): number | undefined {
  let best: number | undefined;
  for (const level of levels) if (best === undefined || Math.abs(level - wanted) < Math.abs(best - wanted)) best = level;
  return best;
}

export function cellAt(cells: LabCell[], level: number, strategy: StrategyKey): LabCell | undefined {
  return cells.find((c) => c.level === level && c.strategy === strategy);
}

export function calibrationAt(entries: LabCalibration[], level: number, model: string): LabCalibration | undefined {
  return entries.find((c) => c.level === level && c.model === model);
}

export function llmAt(levels: LabLlmLevel[], level: number): LabLlmLevel | undefined {
  return levels.find((l) => l.level === level);
}

export type CurveRow = { level: number } & Partial<Record<StrategyKey, number>>;

/** One row per level, one column per non-LLM strategy, for the robustness curve. */
export function curveRows(cells: LabCell[], metric: Metric): CurveRow[] {
  const rows = new Map<number, CurveRow>();
  for (const cell of cells) {
    if (cell.level == null || cell.strategy === "probabilistic_llm") continue;
    const row = rows.get(cell.level) ?? { level: cell.level };
    const value = cell[metric];
    if (value != null) row[cell.strategy as StrategyKey] = value;
    rows.set(cell.level, row);
  }
  return [...rows.values()].sort((a, b) => a.level - b.level);
}

export type LlmPoint = { level: number; value: number; lo: number; hi: number; err: [number, number] };

/** The sampled LLM levels as points with their 95% interval, for the same chart. */
export function llmPoints(levels: LabLlmLevel[], metric: Metric): LlmPoint[] {
  return levels
    .filter((l) => l.level != null)
    .map((l) => {
      const routed = l.strategies.routed;
      const value = routed?.[metric] ?? 0;
      const bounds = routed?.interval?.[metric];
      const lo = bounds?.[0] ?? value;
      const hi = bounds?.[1] ?? value;
      return { level: l.level as number, value, lo, hi, err: [value - lo, hi - value] as [number, number] };
    })
    .sort((a, b) => a.level - b.level);
}
