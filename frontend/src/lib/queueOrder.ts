// The queue's current page order, remembered for the Investigation page so
// an analyst can step to the next or previous item with the keyboard without
// going back to the list. Per tab (sessionStorage), and survives a reload.

const KEY = "concordance.queue.order";

interface QueueOrder {
  ids: string[];
  /** The queue's query string, to go "back to the queue" with filters intact. */
  query: string;
}

export function rememberQueue(order: QueueOrder): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(order));
  } catch {
    // Storage unavailable: next/previous is simply off.
  }
}

export function recallQueue(): QueueOrder {
  try {
    const parsed = JSON.parse(sessionStorage.getItem(KEY) ?? "null") as QueueOrder | null;
    if (parsed && Array.isArray(parsed.ids)) return parsed;
  } catch {
    // fall through
  }
  return { ids: [], query: "" };
}

export function neighbours(id: string): { prev: string | null; next: string | null; position: number; total: number; query: string } {
  const { ids, query } = recallQueue();
  const index = ids.indexOf(id);
  return {
    prev: index > 0 ? (ids[index - 1] ?? null) : null,
    next: index >= 0 && index < ids.length - 1 ? (ids[index + 1] ?? null) : null,
    position: index + 1,
    total: ids.length,
    query,
  };
}
