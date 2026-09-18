import "./index.css";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { RouterProvider } from "react-router/dom";
import { Toaster } from "sonner";

import { ApiError } from "@/api/client";
import { AuthProvider } from "@/auth/AuthProvider";
import { TooltipProvider } from "@/components/ui/overlay";
import { router } from "@/router";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Review data changes as colleagues work; thirty seconds keeps screens
      // fresh without refetching on every focus flicker.
      staleTime: 30_000,
      refetchOnWindowFocus: true,
      // Retry only what a retry can fix. A 4xx is an answer, not a glitch.
      retry: (failures, error) =>
        failures < 2 && !(error instanceof ApiError && error.status >= 400 && error.status < 500),
    },
    mutations: { retry: false },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <TooltipProvider delayDuration={300}>
          <RouterProvider router={router} />
          <Toaster position="bottom-right" richColors closeButton theme="system" />
        </TooltipProvider>
      </AuthProvider>
    </QueryClientProvider>
  </StrictMode>,
);
