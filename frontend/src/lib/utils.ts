import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** Compose class names, letting a later Tailwind utility override an earlier one. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
