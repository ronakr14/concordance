import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";

/**
 * Filter, sort and page state that lives in the URL.
 *
 * The URL is the state, not a copy of it: a filtered queue can be bookmarked,
 * shared with a colleague, and restored by the back button, and a saved view
 * is simply a stored query string. Values equal to their default are dropped
 * from the URL so links stay short.
 */
export function useSearchState<T extends Record<string, string>>(defaults: T) {
  const [params, setParams] = useSearchParams();

  const values = useMemo(() => {
    const out = { ...defaults };
    for (const key of Object.keys(defaults) as (keyof T)[]) {
      const raw = params.get(key as string);
      if (raw !== null) out[key] = raw as T[keyof T];
    }
    return out;
  }, [params, defaults]); // `defaults` is a module-level constant at every call site

  const set = useCallback(
    (patch: Partial<T>, { resetPage = true }: { resetPage?: boolean } = {}) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          for (const [key, value] of Object.entries(patch)) {
            if (value === undefined || value === "" || value === defaults[key]) next.delete(key);
            else next.set(key, value as string);
          }
          // Changing a filter while on page 7 would show an empty page 7.
          if (resetPage && !("offset" in patch)) next.delete("offset");
          return next;
        },
        { replace: true },
      );
    },
    [setParams, defaults],
  );

  const reset = useCallback(() => setParams(new URLSearchParams(), { replace: true }), [setParams]);
  const active = Object.keys(defaults).filter(
    (k) => k !== "offset" && k !== "limit" && k !== "sort" && k !== "order" && values[k] !== defaults[k],
  ).length;

  return { values, set, reset, active, query: params.toString() };
}

/** "" -> undefined, so an unset filter is omitted from the API call. */
export function opt(value: string): string | undefined {
  return value === "" ? undefined : value;
}

export function optNum(value: string): number | undefined {
  if (value === "") return undefined;
  const n = Number(value);
  return Number.isFinite(n) ? n : undefined;
}
