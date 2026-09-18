import { Bot, ChevronRight, User } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";

import type { AuditRow } from "@/api/types";
import { formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";

/** `match.approved` -> `Match approved`. */
export function actionLabel(action: string): string {
  const text = action.replace(/[._]/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

export function Actor({ row }: { row: AuditRow }) {
  if (!row.actor_user_id) {
    return (
      <span className="inline-flex items-center gap-1 text-ink-2">
        <Bot className="size-3.5 text-muted" aria-hidden /> System{row.actor_role && row.actor_role !== "system" ? ` (${row.actor_role})` : ""}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1 text-ink-2">
      <User className="size-3.5 text-muted" aria-hidden />
      {row.actor_email ?? row.actor_user_id.slice(0, 8)}
      {row.actor_role ? <span className="text-muted">· {row.actor_role}</span> : null}
    </span>
  );
}

/** Where an audited entity lives in the app, when it has a page. */
export function entityLink(type: string, id: string): string | null {
  if (type === "match_result") return `/queue/${id}`;
  if (type === "case") return `/cases/${id}`;
  return null;
}

/** A vertical event timeline. Each state change can open its before/after diff. */
export function AuditTimeline({ rows, showEntity = false }: { rows: AuditRow[]; showEntity?: boolean }) {
  return (
    <ol className="relative space-y-0">
      {rows.map((row, i) => (
        <TimelineItem key={row.id} row={row} last={i === rows.length - 1} showEntity={showEntity} />
      ))}
    </ol>
  );
}

function TimelineItem({ row, last, showEntity }: { row: AuditRow; last: boolean; showEntity: boolean }) {
  const [open, setOpen] = useState(false);
  const hasDiff = Boolean(row.before || row.after);
  const link = entityLink(row.entity_type, row.entity_id);
  return (
    <li className="relative grid grid-cols-[1.25rem_1fr] gap-3 pb-4">
      <div className="relative flex justify-center">
        <span className={cn("z-10 mt-1 size-2.5 rounded-full ring-2 ring-surface", row.actor_user_id ? "bg-series-1" : "bg-neutral")} aria-hidden />
        {!last ? <span className="absolute top-3 bottom-[-1rem] w-px bg-border-strong" aria-hidden /> : null}
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
          <span className="text-sm font-medium text-ink">{actionLabel(row.action)}</span>
          <time className="tabular text-xs text-muted" dateTime={row.created_at}>
            {formatDateTime(row.created_at)}
          </time>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs">
          <Actor row={row} />
          {showEntity ? (
            <span className="text-muted">
              {row.entity_type} ·{" "}
              {link ? (
                <Link to={link} className="font-mono text-accent hover:underline">
                  {row.entity_id.slice(0, 8)}
                </Link>
              ) : (
                <span className="font-mono">{row.entity_id.slice(0, 12)}</span>
              )}
            </span>
          ) : null}
          {row.request_id ? <span className="font-mono text-muted">req {row.request_id.slice(0, 12)}</span> : null}
        </div>
        {hasDiff ? (
          <button
            type="button"
            className="mt-1 inline-flex items-center gap-1 text-xs text-accent hover:underline"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            <ChevronRight className={cn("size-3.5 transition-transform", open && "rotate-90")} aria-hidden />
            {row.before ? "What changed" : "Details"}
          </button>
        ) : null}
        {open ? <DiffView before={row.before ?? null} after={row.after ?? null} /> : null}
      </div>
    </li>
  );
}

/** Before and after, key by key. Changed keys are marked; unchanged ones are dimmed. */
export function DiffView({ before, after }: { before: Record<string, unknown> | null; after: Record<string, unknown> | null }) {
  const keys = [...new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])].sort();
  const show = (value: unknown) => (value === undefined ? "" : typeof value === "string" ? value : JSON.stringify(value));
  return (
    <div className="mt-2 overflow-x-auto rounded-md ring-1 ring-border">
      <table className="w-full text-xs">
        <thead className="bg-surface-2 text-left text-muted">
          <tr>
            <th scope="col" className="px-2 py-1 font-medium">Field</th>
            {before ? <th scope="col" className="px-2 py-1 font-medium">Before</th> : null}
            <th scope="col" className="px-2 py-1 font-medium">{before ? "After" : "Value"}</th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {keys.map((key) => {
            const a = before?.[key];
            const b = after?.[key];
            const changed = before !== null && show(a) !== show(b);
            return (
              <tr key={key} className={cn("border-t", before && !changed && "text-muted")}>
                <td className="px-2 py-1 font-sans text-ink-2">{key}</td>
                {before ? <td className={cn("px-2 py-1 break-all", changed && "bg-diverge-neg/10 line-through decoration-diverge-neg/60")}>{show(a) || "—"}</td> : null}
                <td className={cn("px-2 py-1 break-all", changed && "bg-diverge-pos/10")}>{show(b) || "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
