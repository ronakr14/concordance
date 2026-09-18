import { useCallback, useEffect, useState } from "react";

export type ThemeChoice = "light" | "dark" | "system";

const KEY = "concordance.theme";

function read(): ThemeChoice {
  try {
    const stored = localStorage.getItem(KEY);
    return stored === "light" || stored === "dark" ? stored : "system";
  } catch {
    return "system";
  }
}

function apply(choice: ThemeChoice): void {
  const dark =
    choice === "dark" || (choice === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
}

/** The theme choice, persisted per browser; "system" follows the OS live. */
export function useTheme(): [ThemeChoice, (next: ThemeChoice) => void] {
  const [choice, setChoice] = useState<ThemeChoice>(read);

  useEffect(() => {
    apply(choice);
    if (choice !== "system") return;
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => apply("system");
    query.addEventListener("change", onChange);
    return () => query.removeEventListener("change", onChange);
  }, [choice]);

  const set = useCallback((next: ThemeChoice) => {
    try {
      if (next === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, next);
    } catch {
      // Storage unavailable (private window): the choice lasts for this page.
    }
    setChoice(next);
  }, []);

  return [choice, set];
}
