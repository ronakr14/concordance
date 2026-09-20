// Short names for the generated schema types. Nothing here is hand-written
// shape: every type is an alias into `schema.d.ts`, which `make client`
// regenerates from the API's OpenAPI document.

import type { components, paths } from "./schema";

type S = components["schemas"];

export type Paths = paths;

export type User = S["UserOut"];
export type TokenOut = S["TokenOut"];
export type ApiErrorBody = S["ErrorBodyOut"];

export type Kpis = S["KpiOut"];
export type Bucket = S["BucketOut"];
export type VolumePoint = S["VolumePointOut"];

export type MatchListItem = S["MatchListItemOut"];
export type MatchDetail = S["MatchDetailOut"];
export type Candidate = S["CandidateOut"];
export type ProviderBrief = S["ProviderBriefOut"];
export type Band = S["BandOut"];
export type BulkResult = S["BulkOut"];

export type CaseRow = S["CaseListItemOut"];
export type CaseOut = S["CaseOut"];
export type CaseDetail = S["CaseDetailOut"];
export type AuditRow = S["AuditOut"];

export type ProviderRow = S["ProviderListItemOut"];
export type ProviderDetail = S["ProviderDetailOut"];
export type ProviderMatch = S["ProviderMatchOut"];

export type SanctionRecord = S["SanctionRecordOut"];
export type SanctionRecordDetail = S["SanctionRecordDetailOut"];
export type SanctionFile = S["SanctionFileOut"];
export type Inspection = S["InspectionOut"];
export type CommitResult = S["CommitOut"];
export type ColumnMapping = S["ColumnMappingOut"];
export type Facets = S["FacetsOut"];

export type Run = S["RunOut"];
export type RunDiff = S["RunDiffOut"];
export type DiffChange = S["DiffChangeOut"];

export type LabResults = S["LabResultsOut"];
export type LabRun = S["LabRunOut"];
export type LabCell = S["LabCellOut"];
export type LabCalibration = S["LabCalibrationOut"];
export type LabLlmLevel = S["LabLlmLevelOut"];
export type LlmStrategy = S["LlmStrategyOut"];
export type FeedbackRound = S["FeedbackRoundOut"];
export type LabFeedback = S["LabFeedbackOut"];

export type ScoringConfig = S["ScoringConfigOut"];

export type AssistantAnswer = S["AssistantAnswerOut"];
export type AssistantView = S["AssistantViewOut"];
export type AssistantHistory = S["AssistantHistoryOut"];
export type ConfigActivation = S["ConfigActivationOut"];

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}
