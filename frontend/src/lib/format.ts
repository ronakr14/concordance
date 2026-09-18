// Formatting, one way everywhere. Dates from the API are ISO strings: a
// `date` is a calendar day with no zone, a `datetime` is an instant.

const dateFmt = new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "2-digit" });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, {
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
});
const intFmt = new Intl.NumberFormat();
const compactFmt = new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 1 });
const usdFmt = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 });

/** `2026-09-18` -> `Sep 18, 2026`. Parsed as a local calendar day, not UTC midnight. */
export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  const [y, m, d] = value.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return value;
  return dateFmt.format(new Date(y, m - 1, d));
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : dateTimeFmt.format(date);
}

export function formatInt(value: number | null | undefined): string {
  return value == null ? "—" : intFmt.format(value);
}

export function formatCompact(value: number | null | undefined): string {
  return value == null ? "—" : compactFmt.format(value);
}

/** A probability as a percentage with one decimal: 0.9731 -> `97.3%`. */
export function formatPct(value: number | null | undefined, digits = 1): string {
  return value == null ? "—" : `${(value * 100).toFixed(digits)}%`;
}

export function formatUsd(value: number | null | undefined): string {
  return value == null ? "—" : usdFmt.format(value);
}

/** Signed bits, as the Fellegi-Sunter evidence table shows them: `+6.21`, `−3.40`. */
export function formatBits(value: number): string {
  const sign = value > 0 ? "+" : value < 0 ? "−" : "±";
  return `${sign}${Math.abs(value).toFixed(2)}`;
}

/** `license_number` -> `License number`. */
export function humanize(key: string): string {
  const spaced = key.replace(/_/g, " ").trim();
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}

export function shortId(id: string | null | undefined): string {
  return id ? id.slice(0, 8) : "—";
}
