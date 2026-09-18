import { CircleAlert, FolderOpen, RotateCcw } from "lucide-react";
import type { ReactNode } from "react";
import { isRouteErrorResponse, Link, useRouteError } from "react-router";

import { ApiError } from "@/api/client";
import { Button } from "@/components/ui/button";

/** Nothing to show - and what to do about it. */
export function EmptyState({ title, body, action }: { title: string; body?: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      <FolderOpen className="size-8 text-muted" aria-hidden />
      <p className="font-medium text-ink">{title}</p>
      {body ? <p className="max-w-md text-sm text-muted">{body}</p> : null}
      {action ? <div className="mt-2">{action}</div> : null}
    </div>
  );
}

export function describeError(error: unknown): { title: string; message: string; code?: string } {
  if (error instanceof ApiError) {
    if (error.status === 0) return { title: "Can't reach the API", message: error.message, code: error.code };
    if (error.status === 403) return { title: "Not permitted", message: error.message, code: error.code };
    if (error.status === 404) return { title: "Not found", message: error.message, code: error.code };
    return { title: "Something went wrong", message: error.message, code: error.code };
  }
  return { title: "Something went wrong", message: error instanceof Error ? error.message : String(error) };
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const { title, message, code } = describeError(error);
  return (
    <div role="alert" className="flex flex-col items-center gap-2 px-6 py-12 text-center">
      <CircleAlert className="size-8 text-critical-text" aria-hidden />
      <p className="font-medium text-ink">{title}</p>
      <p className="max-w-md text-sm text-muted">{message}</p>
      {code ? <code className="text-xs text-muted">{code}</code> : null}
      {onRetry ? (
        <Button className="mt-2" onClick={onRetry}>
          <RotateCcw /> Try again
        </Button>
      ) : null}
    </div>
  );
}

/** The error boundary every route carries: a crash in one screen never blanks the app. */
export function RouteError() {
  const error = useRouteError();
  if (isRouteErrorResponse(error) && error.status === 404) return <NotFound />;
  return (
    <div className="mx-auto max-w-lg py-16">
      <ErrorState error={error} onRetry={() => window.location.reload()} />
    </div>
  );
}

export function NotFound() {
  return (
    <div className="mx-auto max-w-lg py-16">
      <EmptyState
        title="There is no page here"
        action={
          <Link className="text-accent hover:underline" to="/">
            Back to the dashboard
          </Link>
        }
      />
    </div>
  );
}

export function PageHeader({ title, description, actions }: { title: ReactNode; description?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
        {description ? <p className="mt-1 text-sm text-muted">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
    </div>
  );
}

/** A definition list row, for record detail. Missing values are shown as missing. */
export function Field({ label, children, mono }: { label: string; children: ReactNode; mono?: boolean }) {
  const empty = children == null || children === "";
  return (
    <div className="grid grid-cols-[9rem_1fr] gap-2 py-1 text-sm">
      <dt className="text-muted">{label}</dt>
      <dd className={empty ? "text-muted" : mono ? "font-mono text-xs leading-5 text-ink" : "text-ink"}>{empty ? "—" : children}</dd>
    </div>
  );
}
