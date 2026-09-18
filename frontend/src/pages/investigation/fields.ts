// Which fields each model compares, and how to read each side's value.
//
// Mirrors `matching/comparators.py`: INDIVIDUAL_FIELDS and
// ORGANIZATION_FIELDS. An organization is compared on legal name, DBA and
// EIN; showing it an empty date of birth and first name would be showing the
// reviewer fields the engine never looked at (Q2).

import type { MatchDetail, ProviderBrief } from "@/api/types";

type Record = MatchDetail["sanction_record"];

export interface FieldDef {
  key: string;
  label: string;
  sanction: (r: Record) => string | null;
  provider: (p: ProviderBrief) => string | null;
  mono?: boolean;
}

const join = (...parts: (string | null | undefined)[]) => parts.filter(Boolean).join(", ") || null;

const ADDRESS: FieldDef = {
  key: "address",
  label: "Address",
  sanction: (r) => join(r.address_line1, r.address_line2, r.city),
  provider: (p) => join(p.address_line1, p.address_line2, p.city),
};
const STATE: FieldDef = { key: "state", label: "State", sanction: (r) => r.state, provider: (p) => p.state };
const ZIP: FieldDef = { key: "zip", label: "ZIP", sanction: (r) => r.zip, provider: (p) => p.zip, mono: true };
const NPI: FieldDef = { key: "npi", label: "NPI", sanction: (r) => r.npi, provider: (p) => p.npi, mono: true };

export const INDIVIDUAL_FIELDS: FieldDef[] = [
  NPI,
  { key: "last_name", label: "Last name", sanction: (r) => r.last_name, provider: (p) => p.last_name },
  {
    key: "first_name",
    label: "First name",
    sanction: (r) => join(r.first_name, r.middle_name),
    provider: (p) => join(p.first_name, p.middle_name),
  },
  { key: "dob", label: "Date of birth", sanction: (r) => r.dob ?? null, provider: (p) => p.dob, mono: true },
  ADDRESS,
  STATE,
  ZIP,
  {
    key: "license",
    label: "License",
    sanction: (r) => join(r.license_number, r.license_state),
    provider: (p) => join(p.license_number, p.license_state),
    mono: true,
  },
];

export const ORGANIZATION_FIELDS: FieldDef[] = [
  NPI,
  { key: "ein", label: "EIN", sanction: (r) => r.ein, provider: (p) => p.ein, mono: true },
  {
    key: "legal_name",
    label: "Legal name",
    sanction: (r) => r.organization_name,
    provider: (p) => p.organization_name,
  },
  { key: "dba_alias", label: "DBA", sanction: (r) => r.dba_name, provider: (p) => p.dba_name },
  ADDRESS,
  STATE,
  ZIP,
];

export function fieldsFor(detail: MatchDetail): FieldDef[] {
  const model = (detail.explanation as { model?: string }).model;
  const organization = model ? model === "organization" : detail.sanction_record.is_organization;
  return organization ? ORGANIZATION_FIELDS : INDIVIDUAL_FIELDS;
}

/** One field's term in the Fellegi-Sunter sum, as the engine stored it. */
export interface FieldTerm {
  field: string;
  level: string;
  m: number | null;
  u: number | null;
  weight: number;
}

interface StoredCandidate {
  provider_id?: string;
  field_weights?: { field: string; level_name: string; m: number; u: number; weight: number }[];
}

/**
 * The per-field terms for one candidate.
 *
 * Prefer the explanation's copy, which carries m and u beside the weight; fall
 * back to the candidate row's levels and weights. Either way these are the
 * numbers that produced the decision, not a recomputation.
 */
export function termsFor(detail: MatchDetail, providerId: string): Map<string, FieldTerm> {
  const out = new Map<string, FieldTerm>();
  const stored = ((detail.explanation as { candidates?: StoredCandidate[] }).candidates ?? []).find(
    (c) => c.provider_id === providerId,
  );
  for (const term of stored?.field_weights ?? []) {
    out.set(term.field, { field: term.field, level: term.level_name, m: term.m, u: term.u, weight: term.weight });
  }
  const candidate = detail.candidates.find((c) => c.provider_id === providerId);
  for (const [field, weight] of Object.entries(candidate?.field_weights ?? {})) {
    if (!out.has(field)) {
      out.set(field, { field, level: String(candidate?.field_levels[field] ?? "—"), m: null, u: null, weight });
    }
  }
  return out;
}

/** Agreement level -> a coarse reading for colour and icon. */
export function levelKind(level: string): "agree" | "partial" | "disagree" | "missing" {
  if (level === "MISSING" || level === "BOTH_INVALID") return "missing";
  if (level.includes("DISAGREE") || level === "ONE_INVALID") return "disagree";
  if (level === "EXACT" || level === "VALID_EXACT" || level === "EXACT_SAME_STATE" || level === "ZIP5") return "agree";
  return "partial";
}

export function providerName(p: ProviderBrief | null | undefined): string {
  if (!p) return "—";
  if (p.is_organization) return p.organization_name ?? p.provider_id;
  return [p.first_name, p.middle_name, p.last_name, p.suffix].filter(Boolean).join(" ") || p.provider_id;
}
