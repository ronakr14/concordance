import { fixture, loadState } from "./support";

/** Remove everything the run created. `E2E_KEEP=1` leaves it for a look in the app. */
export default async function globalTeardown(): Promise<void> {
  if (process.env.E2E_KEEP === "1") return;
  const { tag } = loadState();
  fixture("cleanup", tag);
}
