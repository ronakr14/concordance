// The status vocabulary. Defined once; every screen renders a status through
// <StatusBadge>, which reads this table - so a status has exactly one colour,
// one icon and one label everywhere it appears.
//
// Colour comes from the fixed status scale (good / warning / serious /
// critical) plus two quiet tones (info, neutral). It is never the only signal:
// the icon and the label carry the meaning, the colour reinforces it.

import {
  Ban,
  Bot,
  Briefcase,
  CalendarClock,
  Building,
  CircleArrowUp,
  CircleCheck,
  CircleDashed,
  CircleQuestionMark,
  CircleSlash,
  CircleX,
  Clock,
  Equal,
  Hourglass,
  LoaderCircle,
  Lock,
  type LucideIcon,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Sigma,
  Split,
  User,
} from "lucide-react";

export type Tone = "good" | "warning" | "serious" | "critical" | "info" | "neutral";

export interface StatusSpec {
  label: string;
  tone: Tone;
  icon: LucideIcon;
  /** One sentence for the tooltip: what this status means here. */
  hint: string;
}

/** The seven statuses PLAN §8 names, plus the few the workflow adds. */
export const STATUS: Record<string, StatusSpec> = {
  // --- the engine's decision -------------------------------------------------
  MATCH: { label: "Match", tone: "info", icon: CircleCheck, hint: "The engine linked this record to one provider." },
  AMBIGUOUS: {
    label: "Ambiguous",
    tone: "warning",
    icon: CircleQuestionMark,
    hint: "Two or more providers are plausible; a reviewer must choose.",
  },
  UNMATCHED: { label: "Unmatched", tone: "neutral", icon: CircleSlash, hint: "No provider in the master file matches." },
  // --- the reviewer's verdict ------------------------------------------------
  PENDING: { label: "Pending", tone: "neutral", icon: Clock, hint: "Awaiting a reviewer." },
  APPROVED: { label: "Approved", tone: "good", icon: CircleCheck, hint: "A reviewer confirmed the match." },
  REJECTED: { label: "Rejected", tone: "critical", icon: CircleX, hint: "A reviewer ruled the match out." },
  ESCALATED: {
    label: "Escalated",
    tone: "serious",
    icon: CircleArrowUp,
    hint: "An analyst handed this decision to an admin.",
  },
  CASE_CREATED: { label: "Case created", tone: "info", icon: Briefcase, hint: "A compliance case is open." },
  // --- cases -------------------------------------------------------------------
  CASE_PENDING: {
    label: "Pending start",
    tone: "info",
    icon: CalendarClock,
    hint: "Approved and opened; the case window begins on its start date.",
  },
  ACTIVE: { label: "Active", tone: "serious", icon: Briefcase, hint: "The case window is open." },
  EXPIRED: { label: "Expired", tone: "neutral", icon: Hourglass, hint: "The case window ended; closed by the system." },
  CLOSED: { label: "Closed", tone: "neutral", icon: Lock, hint: "An admin closed the case early, with a reason." },
  // --- a provider's derived compliance ----------------------------------------
  EXCLUDED: { label: "Excluded", tone: "critical", icon: ShieldX, hint: "Holds an active compliance case." },
  UNDER_REVIEW: {
    label: "Under review",
    tone: "warning",
    icon: ShieldAlert,
    hint: "The engine's choice on a result no reviewer has decided.",
  },
  CLEAR: { label: "Clear", tone: "good", icon: ShieldCheck, hint: "No active case and nothing pending." },
  // --- runs and files ----------------------------------------------------------
  QUEUED: { label: "Queued", tone: "neutral", icon: Clock, hint: "Waiting for a worker." },
  RUNNING: { label: "Running", tone: "info", icon: LoaderCircle, hint: "A worker is reconciling." },
  COMPLETED: { label: "Completed", tone: "good", icon: CircleCheck, hint: "Finished; results are in the queue." },
  FAILED: { label: "Failed", tone: "critical", icon: CircleX, hint: "The run stopped with an error." },
  CANCELLED: { label: "Cancelled", tone: "neutral", icon: Ban, hint: "Cancelled before it ran." },
  DEAD: { label: "Dead", tone: "critical", icon: CircleX, hint: "Retries exhausted." },
  INSPECTED: { label: "Inspected", tone: "warning", icon: CircleDashed, hint: "Uploaded and inspected; not ingested yet." },
  COMMITTED: { label: "Committed", tone: "good", icon: CircleCheck, hint: "Ingested with a confirmed mapping." },
};

const FALLBACK: StatusSpec = { label: "", tone: "neutral", icon: CircleDashed, hint: "" };

/** The API says `NO_MATCH`; the vocabulary says `UNMATCHED`. */
export function statusKey(raw: string | null | undefined): string {
  if (!raw) return "";
  return raw === "NO_MATCH" ? "UNMATCHED" : raw.toUpperCase();
}

/** A case's phase as a vocabulary key. Its `PENDING` is not the reviewer's `PENDING`. */
export function casePhaseKey(phase: string | null | undefined): string {
  return phase === "PENDING" ? "CASE_PENDING" : (phase ?? "");
}

export function statusSpec(raw: string | null | undefined): StatusSpec {
  const key = statusKey(raw);
  return STATUS[key] ?? { ...FALLBACK, label: key ? key.charAt(0) + key.slice(1).toLowerCase() : "—" };
}

/** Tone -> the Tailwind classes that paint it. Wash background, coloured icon, ink text. */
export const TONE_CLASS: Record<Tone, { chip: string; icon: string; bar: string }> = {
  good: { chip: "bg-good/12 ring-good/30", icon: "text-good-text", bar: "bg-good" },
  warning: { chip: "bg-warning/15 ring-warning/40", icon: "text-warning-text", bar: "bg-warning" },
  serious: { chip: "bg-serious/15 ring-serious/40", icon: "text-serious-text", bar: "bg-serious" },
  critical: { chip: "bg-critical/12 ring-critical/35", icon: "text-critical-text", bar: "bg-critical" },
  info: { chip: "bg-info/10 ring-info/30", icon: "text-info-text", bar: "bg-info" },
  neutral: { chip: "bg-surface-2 ring-border-strong", icon: "text-muted", bar: "bg-neutral" },
};

// --- how the engine reached a decision ---------------------------------------

export const ROUTE: Record<string, { label: string; icon: LucideIcon; hint: string }> = {
  deterministic: { label: "Deterministic", icon: Equal, hint: "Exact identifiers agreed; no scoring needed." },
  probabilistic: { label: "Probabilistic", icon: Sigma, hint: "Fellegi–Sunter weights, calibrated." },
  llm: { label: "LLM adjudicated", icon: Bot, hint: "Inside the grey band; an LLM weighed the evidence." },
};

export function routeSpec(route: string | null | undefined) {
  return ROUTE[route ?? ""] ?? { label: route ?? "—", icon: Split, hint: "" };
}

export const RECORD_TYPE: Record<"individual" | "organization", { label: string; icon: LucideIcon }> = {
  individual: { label: "Individual", icon: User },
  organization: { label: "Organization", icon: Building },
};
