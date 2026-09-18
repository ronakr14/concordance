import { Bookmark, Search, Trash2, X } from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/form";
import {
  DialogContent,
  DialogRoot,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuRoot,
  MenuSeparator,
  MenuTrigger,
} from "@/components/ui/overlay";

/** One row of filters above a table, with a reset that says how many are on. */
export function FilterBar({ children, active, onReset }: { children: ReactNode; active: number; onReset: () => void }) {
  return (
    <div className="mb-3 flex flex-wrap items-end gap-2 rounded-lg bg-surface px-3 py-2.5 ring-1 ring-border" role="search">
      {children}
      {active > 0 ? (
        <Button size="sm" variant="ghost" onClick={onReset} className="ml-auto">
          <X /> Clear {active} filter{active === 1 ? "" : "s"}
        </Button>
      ) : null}
    </div>
  );
}

export function FilterField({ label, htmlFor, children, width = "w-40" }: { label: string; htmlFor: string; children: ReactNode; width?: string }) {
  return (
    <div className={`flex flex-col gap-1 ${width}`}>
      <Label htmlFor={htmlFor}>{label}</Label>
      {children}
    </div>
  );
}

/** A search box that commits after typing pauses, not on every keystroke. */
export function SearchInput({ id, value, onChange, placeholder }: { id: string; value: string; onChange: (v: string) => void; placeholder: string }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  useEffect(() => {
    if (draft === value) return;
    const timer = setTimeout(() => onChange(draft.trim()), 300);
    return () => clearTimeout(timer);
  }, [draft, value, onChange]);
  return (
    <div className="relative">
      <Search className="pointer-events-none absolute left-2 top-1/2 size-4 -translate-y-1/2 text-muted" aria-hidden />
      <Input id={id} type="search" className="pl-7" value={draft} placeholder={placeholder} onChange={(e) => setDraft(e.target.value)} />
    </div>
  );
}

// --- saved views --------------------------------------------------------------

interface SavedView {
  name: string;
  query: string;
}

function readViews(key: string): SavedView[] {
  try {
    return JSON.parse(localStorage.getItem(key) ?? "[]") as SavedView[];
  } catch {
    return [];
  }
}

/**
 * Named filter sets, per browser. A view is just the URL query string, so
 * saving one costs nothing and applying one is a navigation.
 */
export function SavedViews({ storageKey, query, onApply }: { storageKey: string; query: string; onApply: (query: string) => void }) {
  const key = `concordance.views.${storageKey}`;
  const [views, setViews] = useState<SavedView[]>(() => readViews(key));

  const persist = (next: SavedView[]) => {
    setViews(next);
    try {
      localStorage.setItem(key, JSON.stringify(next));
    } catch {
      // Unavailable storage: views last for this page only.
    }
  };

  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const save = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;
    persist([...views.filter((v) => v.name !== trimmed), { name: trimmed, query }]);
    setNaming(false);
    setName("");
  };

  return (
    <>
    <DialogRoot open={naming} onOpenChange={setNaming}>
      <DialogContent title="Save this view" description="The current filters and sort, under a name you choose.">
        <form onSubmit={save} className="space-y-3">
          <Input autoFocus aria-label="View name" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Ambiguous, Texas, this week" />
          <div className="flex justify-end">
            <Button type="submit" variant="primary" disabled={!name.trim()}>
              Save view
            </Button>
          </div>
        </form>
      </DialogContent>
    </DialogRoot>
    <MenuRoot>
      <MenuTrigger asChild>
        <Button size="sm" variant="ghost">
          <Bookmark /> Views{views.length ? ` (${views.length})` : ""}
        </Button>
      </MenuTrigger>
      <MenuContent className="min-w-60">
        <MenuLabel>Saved views</MenuLabel>
        {views.length === 0 ? <p className="px-2 py-1.5 text-xs text-muted">None yet. Filter the table, then save it.</p> : null}
        {views.map((view) => (
          <MenuItem key={view.name} onSelect={() => onApply(view.query)} className="justify-between">
            <span className="truncate">{view.name}</span>
            <button
              type="button"
              className="rounded-sm p-0.5 text-muted hover:text-critical-text"
              aria-label={`Delete view ${view.name}`}
              onClick={(event) => {
                event.stopPropagation();
                persist(views.filter((v) => v.name !== view.name));
              }}
            >
              <Trash2 className="size-3.5" />
            </button>
          </MenuItem>
        ))}
        <MenuSeparator />
        <MenuItem onSelect={() => setNaming(true)} disabled={!query}>
          <Bookmark /> Save current filters…
        </MenuItem>
      </MenuContent>
    </MenuRoot>
    </>
  );
}
