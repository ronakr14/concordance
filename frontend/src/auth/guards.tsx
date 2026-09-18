import type { ReactNode } from "react";
import { Navigate, Outlet, useLocation } from "react-router";

import { useAuth, useIsAdmin } from "./AuthProvider";

/** Protected routes. An anonymous visitor goes to /login and comes back after. */
export function RequireAuth() {
  const { status } = useAuth();
  const location = useLocation();
  if (status === "restoring") return <BootScreen />;
  if (status === "anonymous") {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <Outlet />;
}

/** An admin-only route. An analyst is sent home rather than shown a 403 page. */
export function RequireAdmin() {
  return useIsAdmin() ? <Outlet /> : <Navigate to="/" replace />;
}

/**
 * Render children only for an admin.
 *
 * The point is not security - the API refuses the action either way - but
 * honesty: an analyst should never be offered a button that can only fail.
 */
export function AdminOnly({ children, fallback = null }: { children: ReactNode; fallback?: ReactNode }) {
  return useIsAdmin() ? children : fallback;
}

function BootScreen() {
  return (
    <div className="grid h-full place-items-center text-muted" role="status" aria-live="polite">
      Restoring your session…
    </div>
  );
}
