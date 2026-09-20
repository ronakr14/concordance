// Every server read and write the app makes, as TanStack Query hooks.
//
// One file so the cache keys live together: a mutation's `invalidate` list
// can be checked against the reads it affects at a glance. Query parameters
// are typed from the generated schema, so a renamed filter is a type error.

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, unwrap, uploadForm } from "./client";
import type { Inspection, Paths } from "./types";

type Query<P extends keyof Paths, M extends "get"> = Paths[P][M] extends { parameters: { query?: infer Q } }
  ? NonNullable<Q>
  : never;
type Body<P extends keyof Paths, M extends "post" | "put"> = Paths[P][M] extends {
  requestBody?: { content: { "application/json": infer B } };
}
  ? B
  : never;

export type MatchFilters = Query<"/matches", "get">;
export type CaseFilters = Query<"/cases", "get">;
export type AuditFilters = Query<"/audit", "get">;
export type ProviderFilters = Query<"/providers", "get">;
export type SanctionFilters = Query<"/sanctions", "get">;
export type RunIn = Body<"/reconciliation/run", "post">;
export type CommitIn = Body<"/sanctions/upload/{file_id}/commit", "post">;
export type ReviewIn = Body<"/matches/{match_id}/approve", "post">;
export type LabSweepIn = Body<"/lab/sweep", "post">;
export type LabLlmIn = Body<"/lab/llm", "post">;
export type LabFeedbackIn = Body<"/lab/feedback", "post">;
export type RetuneIn = Body<"/scoring-configs/retune", "post">;

export const keys = {
  stats: ["stats"] as const,
  matches: ["matches"] as const,
  match: (id: string) => ["matches", "detail", id] as const,
  cases: ["cases"] as const,
  case: (id: string) => ["cases", "detail", id] as const,
  audit: ["audit"] as const,
  providers: ["providers"] as const,
  provider: (id: string) => ["providers", "detail", id] as const,
  sanctions: ["sanctions"] as const,
  files: ["sanction-files"] as const,
  facets: ["facets"] as const,
  mappings: ["column-mappings"] as const,
  runs: ["runs"] as const,
  lab: ["lab"] as const,
  configs: ["scoring-configs"] as const,
};

/** Paged lists keep the previous page on screen while the next loads. */
const paged = { placeholderData: keepPreviousData } as const;

// --- dashboard ------------------------------------------------------------------

export const useKpis = () => useQuery({ queryKey: [...keys.stats, "kpis"], queryFn: () => unwrap(api.GET("/stats/kpis")) });

export const useConfidenceDistribution = () =>
  useQuery({ queryKey: [...keys.stats, "confidence"], queryFn: () => unwrap(api.GET("/stats/confidence-distribution")) });

export const useStateDistribution = () =>
  useQuery({ queryKey: [...keys.stats, "state"], queryFn: () => unwrap(api.GET("/stats/state-distribution")) });

export const useCaseStatus = () =>
  useQuery({ queryKey: [...keys.stats, "case-status"], queryFn: () => unwrap(api.GET("/stats/case-status")) });

export const useVolume = (days: number, bucket: "day" | "week" | "month") =>
  useQuery({
    queryKey: [...keys.stats, "volume", days, bucket],
    queryFn: () => unwrap(api.GET("/stats/reconciliation-volume", { params: { query: { days, bucket } } })),
  });

// --- matches ----------------------------------------------------------------------

export const useMatches = (filters: MatchFilters) =>
  useQuery({
    queryKey: [...keys.matches, "list", filters],
    queryFn: () => unwrap(api.GET("/matches", { params: { query: filters } })),
    ...paged,
  });

export const useMatch = (id: string) =>
  useQuery({
    queryKey: keys.match(id),
    queryFn: () => unwrap(api.GET("/matches/{match_id}", { params: { path: { match_id: id } } })),
  });

/** After any verdict: the queue, the dashboard, cases and audit all moved. */
function useInvalidateReview() {
  const client = useQueryClient();
  return () =>
    Promise.all(
      [keys.matches, keys.stats, keys.cases, keys.audit, keys.providers].map((queryKey) =>
        client.invalidateQueries({ queryKey }),
      ),
    );
}

export function useApprove(id: string) {
  const invalidate = useInvalidateReview();
  return useMutation({
    mutationFn: (body: ReviewIn) =>
      unwrap(api.POST("/matches/{match_id}/approve", { params: { path: { match_id: id } }, body })),
    onSuccess: invalidate,
  });
}

export function useReject(id: string) {
  const invalidate = useInvalidateReview();
  return useMutation({
    mutationFn: (body: { comment: string; provider_id?: string | null }) =>
      unwrap(api.POST("/matches/{match_id}/reject", { params: { path: { match_id: id } }, body })),
    onSuccess: invalidate,
  });
}

export function useEscalate(id: string) {
  const invalidate = useInvalidateReview();
  return useMutation({
    mutationFn: (body: { comment: string }) =>
      unwrap(api.POST("/matches/{match_id}/escalate", { params: { path: { match_id: id } }, body })),
    onSuccess: invalidate,
  });
}

export function useBulkReview() {
  const invalidate = useInvalidateReview();
  return useMutation({
    mutationFn: (body: { ids: string[]; action: "reject" | "escalate"; comment: string }) =>
      unwrap(api.POST("/matches/bulk", { body })),
    onSuccess: invalidate,
  });
}

// --- cases ---------------------------------------------------------------------------

export const useCases = (filters: CaseFilters) =>
  useQuery({
    queryKey: [...keys.cases, "list", filters],
    queryFn: () => unwrap(api.GET("/cases", { params: { query: filters } })),
    ...paged,
  });

export const useCase = (id: string) =>
  useQuery({
    queryKey: keys.case(id),
    queryFn: () => unwrap(api.GET("/cases/{case_id}", { params: { path: { case_id: id } } })),
  });

export const useCaseAudit = (id: string) =>
  useQuery({
    queryKey: [...keys.case(id), "audit"],
    queryFn: () =>
      unwrap(api.GET("/cases/{case_id}/audit", { params: { path: { case_id: id }, query: { limit: 200 } } })),
  });

export function useCloseCase(id: string) {
  const invalidate = useInvalidateReview();
  return useMutation({
    mutationFn: (reason: string) =>
      unwrap(api.POST("/cases/{case_id}/close", { params: { path: { case_id: id } }, body: { reason } })),
    onSuccess: invalidate,
  });
}

// --- audit -----------------------------------------------------------------------------

export const useAudit = (filters: AuditFilters, enabled = true) =>
  useQuery({
    queryKey: [...keys.audit, filters],
    queryFn: () => unwrap(api.GET("/audit", { params: { query: filters } })),
    enabled,
    ...paged,
  });

// --- providers -------------------------------------------------------------------------

export const useProviders = (filters: ProviderFilters) =>
  useQuery({
    queryKey: [...keys.providers, "list", filters],
    queryFn: () => unwrap(api.GET("/providers", { params: { query: filters } })),
    ...paged,
  });

export const useProvider = (id: string | null) =>
  useQuery({
    queryKey: keys.provider(id ?? ""),
    queryFn: () => unwrap(api.GET("/providers/{provider_id}", { params: { path: { provider_id: id ?? "" } } })),
    enabled: Boolean(id),
  });

// --- sanctions ------------------------------------------------------------------------

export const useSanctions = (filters: SanctionFilters) =>
  useQuery({
    queryKey: [...keys.sanctions, "list", filters],
    queryFn: () => unwrap(api.GET("/sanctions", { params: { query: filters } })),
    ...paged,
  });

export const useSanctionFiles = (offset: number, limit: number) =>
  useQuery({
    queryKey: [...keys.files, offset, limit],
    queryFn: () => unwrap(api.GET("/sanctions/files", { params: { query: { offset, limit } } })),
    ...paged,
  });

export const useFacets = () =>
  useQuery({ queryKey: keys.facets, queryFn: () => unwrap(api.GET("/sanctions/facets")), staleTime: 5 * 60_000 });

export const useMappings = (sourceAuthority?: string) =>
  useQuery({
    queryKey: [...keys.mappings, sourceAuthority ?? ""],
    queryFn: () =>
      unwrap(api.GET("/column-mappings", { params: { query: { source_authority: sourceAuthority || undefined } } })),
  });

export function useInspectUpload() {
  return useMutation({
    mutationFn: ({ file, sourceAuthority, onProgress }: { file: File; sourceAuthority?: string; onProgress: (f: number) => void }) => {
      const form = new FormData();
      form.append("file", file);
      if (sourceAuthority) form.append("source_authority", sourceAuthority);
      return uploadForm<Inspection>("/sanctions/upload", form, onProgress);
    },
  });
}

export function useCommitUpload(fileId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: CommitIn) =>
      unwrap(api.POST("/sanctions/upload/{file_id}/commit", { params: { path: { file_id: fileId } }, body })),
    onSuccess: () =>
      Promise.all(
        [keys.sanctions, keys.files, keys.facets, keys.mappings, keys.stats].map((queryKey) =>
          client.invalidateQueries({ queryKey }),
        ),
      ),
  });
}

// --- runs --------------------------------------------------------------------------

const LIVE = new Set(["QUEUED", "RUNNING"]);

export const useRuns = (fileId?: string) =>
  useQuery({
    queryKey: [...keys.runs, fileId ?? "all"],
    queryFn: () => unwrap(api.GET("/reconciliation/runs", { params: { query: { file_id: fileId, limit: 20 } } })),
    // Poll while anything is in flight, and stop once nothing is.
    refetchInterval: (query) => (query.state.data?.items.some((r) => LIVE.has(r.status)) ? 2000 : false),
  });

export function useStartRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: RunIn) => unwrap(api.POST("/reconciliation/run", { body })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.runs }),
  });
}

export function useCancelRun() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) =>
      unwrap(api.POST("/reconciliation/runs/{run_id}/cancel", { params: { path: { run_id: runId } } })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.runs }),
  });
}

// --- lab ----------------------------------------------------------------------------

/**
 * The newest completed sweep and its LLM run. Polls while an experiment is
 * live, so progress moves and the curves swap in when it finishes.
 */
export const useLabResults = () =>
  useQuery({
    queryKey: [...keys.lab, "results"],
    queryFn: () => unwrap(api.GET("/lab/results")),
    refetchInterval: (query) => (query.state.data?.live ? 3000 : false),
  });

export function useStartSweep() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: LabSweepIn) => unwrap(api.POST("/lab/sweep", { body })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.lab }),
  });
}

export function useStartLlm() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: LabLlmIn) => unwrap(api.POST("/lab/llm", { body })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.lab }),
  });
}

/** Simulated review rounds. Polls while any Lab experiment is live. */
export const useLabFeedback = () =>
  useQuery({
    queryKey: [...keys.lab, "feedback"],
    queryFn: () => unwrap(api.GET("/lab/feedback")),
    refetchInterval: (query) => (query.state.data?.live ? 3000 : false),
  });

export function useStartFeedback() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: LabFeedbackIn) => unwrap(api.POST("/lab/feedback", { body })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.lab }),
  });
}

// --- scoring configs ------------------------------------------------------------------

export const useConfigs = () =>
  useQuery({ queryKey: [...keys.configs, "list"], queryFn: () => unwrap(api.GET("/scoring-configs")) });

export const useActivations = () =>
  useQuery({ queryKey: [...keys.configs, "activations"], queryFn: () => unwrap(api.GET("/scoring-configs/activations")) });

export function useRetune() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: RetuneIn) => unwrap(api.POST("/scoring-configs/retune", { body })),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.configs }),
  });
}

export function useActivateConfig() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, reason }: { id: string; reason?: string }) =>
      unwrap(
        api.POST("/scoring-configs/{config_id}/activate", {
          params: { path: { config_id: id } },
          body: { reason: reason ?? null },
        }),
      ),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.configs });
      void client.invalidateQueries({ queryKey: keys.runs });
    },
  });
}
