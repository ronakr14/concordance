import { ArrowRight, CircleArrowUp, Clock, GitCompare } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { useCaseStatus, useConfidenceDistribution, useKpis, useStateDistribution, useVolume } from "@/api/queries";
import type { Kpis } from "@/api/types";
import { ChartCard, ChartTooltip, MiniTable, useChartTheme } from "@/components/ChartCard";
import { ErrorState, PageHeader } from "@/components/states";
import { StatusBadge } from "@/components/status";
import { Select } from "@/components/ui/form";
import { Card, CardBody, CardHeader, Skeleton } from "@/components/ui/surface";
import { formatCompact, formatInt, formatUsd } from "@/lib/format";
import { statusSpec } from "@/lib/status";

const TILES: { key: keyof Kpis; label: string; to?: string }[] = [
  { key: "providers", label: "Providers", to: "/providers" },
  { key: "sanction_records", label: "Sanction records", to: "/sanctions" },
  { key: "matched", label: "Matched", to: "/queue?review_status=&decision=MATCH" },
  { key: "no_match", label: "Unmatched", to: "/queue?review_status=&decision=NO_MATCH" },
  { key: "ambiguous", label: "Ambiguous", to: "/queue?review_status=&decision=AMBIGUOUS" },
  { key: "pending_review", label: "Pending review", to: "/queue" },
  { key: "approved", label: "Approved", to: "/queue?review_status=APPROVED" },
  { key: "cases_active", label: "Cases created (active)", to: "/cases?status=ACTIVE" },
];

export function DashboardPage() {
  const kpis = useKpis();
  return (
    <>
      <PageHeader title="Dashboard" description="Where the reconciliation stands, and where the review work is." />
      {kpis.error ? (
        <Card className="mb-4">
          <ErrorState error={kpis.error} onRetry={() => void kpis.refetch()} />
        </Card>
      ) : (
        <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          {TILES.map((tile) => (
            <StatTile key={tile.key} label={tile.label} value={kpis.data?.[tile.key]} to={tile.to} />
          ))}
        </div>
      )}
      <div className="grid gap-4 lg:grid-cols-3">
        <div className="space-y-4 lg:col-span-2">
          <VolumeChart />
          <div className="grid gap-4 md:grid-cols-2">
            <ConfidenceChart />
            <CaseStatusChart />
          </div>
        </div>
        <div className="space-y-4">
          <Workload kpis={kpis.data} loading={kpis.isLoading} />
          <StateChart />
        </div>
      </div>
    </>
  );
}

function StatTile({ label, value, to }: { label: string; value: number | undefined; to?: string }) {
  const body = (
    <div className="rounded-lg bg-surface px-4 py-3 ring-1 ring-border transition-colors hover:ring-border-strong">
      <p className="text-xs text-muted">{label}</p>
      {value === undefined ? (
        <Skeleton className="mt-1.5 h-7 w-20" />
      ) : (
        <p className="mt-0.5 text-2xl font-semibold tracking-tight text-ink" title={formatInt(value)}>
          {formatCompact(value)}
        </p>
      )}
    </div>
  );
  return to ? (
    <Link to={to} className="block rounded-lg focus-visible:outline-2">
      {body}
    </Link>
  ) : (
    body
  );
}

/** The operational summary: the work waiting, each item a link to it. */
function Workload({ kpis, loading }: { kpis: Kpis | undefined; loading: boolean }) {
  const rows = [
    { icon: Clock, label: "Awaiting a reviewer", value: kpis?.pending_review, to: "/queue" },
    { icon: CircleArrowUp, label: "Escalated to an admin", value: kpis?.escalated, to: "/queue?review_status=ESCALATED" },
    { icon: GitCompare, label: "Conflicts on live cases", value: kpis?.conflicts, to: "/queue?review_status=&conflict=true" },
  ];
  const decided = (kpis?.approved ?? 0) + (kpis?.rejected ?? 0);
  const open = (kpis?.pending_review ?? 0) + (kpis?.escalated ?? 0);
  const progress = decided + open > 0 ? decided / (decided + open) : 0;
  return (
    <Card>
      <CardHeader title="Workload" description="What needs a person next." />
      <CardBody className="space-y-3">
        {rows.map((row) => (
          <Link key={row.label} to={row.to} className="flex items-center gap-3 rounded-md px-2 py-1.5 hover:bg-surface-2">
            <row.icon className="size-4 text-muted" aria-hidden />
            <span className="flex-1 text-sm text-ink-2">{row.label}</span>
            {loading ? <Skeleton className="h-5 w-10" /> : <span className="tabular text-sm font-semibold text-ink">{formatInt(row.value)}</span>}
            <ArrowRight className="size-3.5 text-muted" aria-hidden />
          </Link>
        ))}
        <div className="border-t pt-3">
          <div className="mb-1 flex justify-between text-xs text-muted">
            <span>Review progress</span>
            <span className="tabular">
              {formatInt(decided)} decided · {formatInt(open)} open
            </span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-accent-wash" role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress * 100)} aria-label="Share of proposed matches a reviewer has decided">
            <div className="h-full rounded-full bg-series-1" style={{ width: `${progress * 100}%` }} />
          </div>
        </div>
      </CardBody>
    </Card>
  );
}

function VolumeChart() {
  const [bucket, setBucket] = useState<"day" | "week" | "month">("day");
  const days = bucket === "day" ? 30 : bucket === "week" ? 180 : 366;
  const volume = useVolume(days, bucket);
  const t = useChartTheme();
  const data = volume.data ?? [];
  const empty = data.every((p) => p.records === 0);

  return (
    <ChartCard
      title="Reconciliation volume"
      description={`Records reconciled and matched per ${bucket}, last ${days} days`}
      label={`Line chart of records reconciled and matched per ${bucket}`}
      loading={volume.isLoading}
      error={volume.error}
      onRetry={() => void volume.refetch()}
      empty={empty}
      height={260}
      actions={
        <Select className="w-24" aria-label="Bucket size" value={bucket} onChange={(e) => setBucket(e.target.value as typeof bucket)}>
          <option value="day">Daily</option>
          <option value="week">Weekly</option>
          <option value="month">Monthly</option>
        </Select>
      }
      table={
        <MiniTable
          head={["Period", "Runs", "Records", "Matched", "LLM cost"]}
          rows={data.map((p) => [p.period, formatInt(p.runs), formatInt(p.records), formatInt(p.matched), formatUsd(p.llm_cost_usd)])}
        />
      }
      chart={
        <ResponsiveContainer>
          <LineChart data={data} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={t.grid} vertical={false} />
            <XAxis dataKey="period" tick={{ fill: t.muted, fontSize: 11 }} tickLine={false} axisLine={{ stroke: t.axis }} minTickGap={24} />
            <YAxis tick={{ fill: t.muted, fontSize: 11 }} tickLine={false} axisLine={false} width={44} tickFormatter={(v: number) => formatCompact(v)} allowDecimals={false} />
            <Tooltip
              cursor={{ stroke: t.axis }}
              content={({ active, payload, label }) => (
                <ChartTooltip
                  active={active}
                  label={label as string}
                  items={(payload ?? []).map((p) => ({ name: String(p.name), value: formatInt(Number(p.value)), color: String(p.color) }))}
                />
              )}
            />
            <Legend verticalAlign="top" align="right" height={24} iconType="plainline" wrapperStyle={{ fontSize: 12, color: t.ink2 }} />
            <Line type="monotone" dataKey="records" name="Reconciled" stroke={t.series1} strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: t.surface, strokeWidth: 2 }} />
            <Line type="monotone" dataKey="matched" name="Matched" stroke={t.series2} strokeWidth={2} dot={false} activeDot={{ r: 4, stroke: t.surface, strokeWidth: 2 }} />
          </LineChart>
        </ResponsiveContainer>
      }
    />
  );
}

function ConfidenceChart() {
  const dist = useConfidenceDistribution();
  const t = useChartTheme();
  const data = dist.data ?? [];
  return (
    <ChartCard
      title="Confidence distribution"
      description="Current results by calibrated confidence"
      label="Column chart of current results per ten-point confidence bucket"
      loading={dist.isLoading}
      error={dist.error}
      onRetry={() => void dist.refetch()}
      empty={data.every((b) => b.count === 0)}
      table={<MiniTable head={["Confidence", "Results"]} rows={data.map((b) => [b.label, formatInt(b.count)])} />}
      chart={
        <ResponsiveContainer>
          <BarChart data={data} margin={{ top: 8, right: 4, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={t.grid} vertical={false} />
            <XAxis dataKey="label" tick={{ fill: t.muted, fontSize: 10 }} tickLine={false} axisLine={{ stroke: t.axis }} tickFormatter={(l: string) => l.split("-")[0] ?? l} />
            <YAxis tick={{ fill: t.muted, fontSize: 11 }} tickLine={false} axisLine={false} width={40} tickFormatter={(v: number) => formatCompact(v)} allowDecimals={false} />
            <Tooltip
              cursor={{ fill: t.hover }}
              content={({ active, payload, label }) => (
                <ChartTooltip active={active} label={`Confidence ${label}`} items={(payload ?? []).map((p) => ({ name: "Results", value: formatInt(Number(p.value)), color: t.series1 }))} />
              )}
            />
            <Bar dataKey="count" fill={t.series1} maxBarSize={24} radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      }
    />
  );
}

function CaseStatusChart() {
  const status = useCaseStatus();
  const t = useChartTheme();
  const data = (status.data ?? []).map((b) => ({ ...b, name: statusSpec(b.label).label }));
  return (
    <ChartCard
      title="Case status"
      description="Every case, by where it stands"
      label="Bar chart of cases per status"
      loading={status.isLoading}
      error={status.error}
      onRetry={() => void status.refetch()}
      empty={data.every((b) => b.count === 0)}
      table={
        <table className="w-full text-sm">
          <tbody>
            {data.map((b) => (
              <tr key={b.label} className="border-t first:border-t-0">
                <td className="py-1.5">
                  <StatusBadge status={b.label} />
                </td>
                <td className="tabular py-1.5 text-right">{formatInt(b.count)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      }
      chart={
        <ResponsiveContainer>
          <BarChart data={data} layout="vertical" margin={{ top: 4, right: 36, bottom: 0, left: 0 }}>
            <XAxis type="number" hide />
            <YAxis type="category" dataKey="name" tick={{ fill: t.ink2, fontSize: 12 }} tickLine={false} axisLine={{ stroke: t.axis }} width={72} />
            <Tooltip
              cursor={{ fill: t.hover }}
              content={({ active, payload, label }) => (
                <ChartTooltip active={active} label={label as string} items={(payload ?? []).map((p) => ({ name: "Cases", value: formatInt(Number(p.value)), color: t.series1 }))} />
              )}
            />
            <Bar dataKey="count" fill={t.series1} maxBarSize={24} radius={[0, 4, 4, 0]} label={{ position: "right", fill: t.ink2, fontSize: 11 }} />
          </BarChart>
        </ResponsiveContainer>
      }
    />
  );
}

const STATE_LIMIT = 12;

function StateChart() {
  const dist = useStateDistribution();
  const t = useChartTheme();
  const all = [...(dist.data ?? [])].sort((a, b) => b.count - a.count);
  const top = all.slice(0, STATE_LIMIT);
  const rest = all.slice(STATE_LIMIT).reduce((sum, b) => sum + b.count, 0);
  const data = rest > 0 ? [...top, { label: "Other", count: rest }] : top;
  return (
    <ChartCard
      title="State distribution"
      description={`Current results by the sanction record's state, top ${STATE_LIMIT}`}
      label="Bar chart of current results per state"
      loading={dist.isLoading}
      error={dist.error}
      onRetry={() => void dist.refetch()}
      empty={all.length === 0}
      height={Math.max(200, data.length * 24)}
      table={<MiniTable head={["State", "Results"]} rows={all.map((b) => [b.label, formatInt(b.count)])} />}
      chart={
        <ResponsiveContainer>
          <BarChart data={data} layout="vertical" margin={{ top: 0, right: 40, bottom: 0, left: 0 }} barCategoryGap={4}>
            <XAxis type="number" hide />
            <YAxis type="category" dataKey="label" tick={{ fill: t.ink2, fontSize: 11 }} tickLine={false} axisLine={{ stroke: t.axis }} width={48} interval={0} />
            <Tooltip
              cursor={{ fill: t.hover }}
              content={({ active, payload, label }) => (
                <ChartTooltip active={active} label={label as string} items={(payload ?? []).map((p) => ({ name: "Results", value: formatInt(Number(p.value)), color: t.series1 }))} />
              )}
            />
            <Bar dataKey="count" fill={t.series1} maxBarSize={16} radius={[0, 4, 4, 0]} label={{ position: "right", fill: t.ink2, fontSize: 10 }} />
          </BarChart>
        </ResponsiveContainer>
      }
    />
  );
}
