import type { ComponentType } from "react";
import { createBrowserRouter, type RouteObject } from "react-router";

import { RequireAdmin, RequireAuth } from "@/auth/guards";
import { AppShell } from "@/components/AppShell";
import { NotFound, RouteError } from "@/components/states";
import { LoginPage } from "@/pages/Login";

/**
 * A page loaded on first visit rather than in the initial bundle. Recharts
 * alone is a third of the app; nobody signing in should download it.
 */
function page(load: () => Promise<Record<string, unknown>>, name: string): Pick<RouteObject, "errorElement" | "lazy"> {
  return {
    errorElement: <RouteError />,
    lazy: async () => ({ Component: (await load())[name] as ComponentType }),
  };
}

// Every route carries its own error boundary, so a crash in one screen shows
// an error in that screen and leaves the navigation working.
export const router = createBrowserRouter([
  { path: "/login", element: <LoginPage />, errorElement: <RouteError /> },
  {
    element: <RequireAuth />,
    errorElement: <RouteError />,
    children: [
      {
        element: <AppShell />,
        errorElement: <RouteError />,
        children: [
          { index: true, ...page(() => import("@/pages/Dashboard"), "DashboardPage") },
          { path: "providers", ...page(() => import("@/pages/Providers"), "ProvidersPage") },
          { path: "providers/:providerId", ...page(() => import("@/pages/ProviderProfile"), "ProviderPage") },
          { path: "sanctions", ...page(() => import("@/pages/Sanctions"), "SanctionsPage") },
          { path: "sanctions/upload", ...page(() => import("@/pages/Upload"), "UploadPage") },
          { path: "queue", ...page(() => import("@/pages/Queue"), "QueuePage") },
          { path: "queue/:matchId", ...page(() => import("@/pages/Investigation"), "InvestigationPage") },
          { path: "cases", ...page(() => import("@/pages/Cases"), "CasesPage") },
          { path: "cases/:caseId", ...page(() => import("@/pages/CaseDetail"), "CaseDetailPage") },
          { path: "lab", ...page(() => import("@/pages/Lab"), "LabPage") },
          {
            element: <RequireAdmin />,
            children: [{ path: "audit", ...page(() => import("@/pages/Audit"), "AuditPage") }],
          },
          { path: "*", element: <NotFound /> },
        ],
      },
    ],
  },
]);
