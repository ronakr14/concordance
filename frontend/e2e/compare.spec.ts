/**
 * GATE 9 (run comparison): the page picks two runs, counts what moved, names
 * the provenance that explains it, and links a changed decision to its
 * evidence.
 *
 * It compares whatever two runs are newest rather than making its own - a run
 * hashes the 50,000-provider snapshot, and the API tests already prove the
 * diff. With fewer than two runs in the database it asserts the empty state
 * instead, so the suite is honest on a fresh install rather than skipped.
 */
import path from "node:path";

import { expect, expectFits, loadState, ROOT, type RunState, signIn, test } from "./support";

test.describe.configure({ mode: "serial" });

let state: RunState;
test.beforeAll(() => {
  state = loadState();
});

const SHOTS = path.join(ROOT, "reports", "e2e", "compare");

test("comparing two runs says what moved and why", async ({ page }, info) => {
  await signIn(page, state.admin);
  await page.getByRole("link", { name: "Compare" }).click();
  await expect(page).toHaveURL(/\/compare$/);
  await expect(page.getByRole("heading", { name: "Compare runs" })).toBeVisible();

  const earlier = page.getByLabel("Earlier run");
  const later = page.getByLabel("Later run");
  const runs = await earlier.locator("option").count();
  if (runs < 3) {
    // Two options at most means fewer than two runs: "Pick a run" plus one.
    await expect(page.getByText("Two runs are needed")).toBeVisible();
    return;
  }

  // The two newest are picked by default, and the summary describes them both.
  await expect(page.getByText(/records decided by both runs/)).toBeVisible();
  await expect(page.getByRole("term").first()).toBeVisible();
  await expect(page.getByText("Changed decision")).toBeVisible();
  await expect(page.getByRole("heading", { name: "What differs between the runs" })).toBeVisible();

  // Comparing a run with itself is refused rather than drawn as all-zero.
  const first = await earlier.inputValue();
  await later.selectOption(first);
  await expect(page.getByText("Pick two different runs")).toBeVisible();

  // Back to two different runs, and the threshold drives the confidence list.
  const others = await earlier.locator("option").evaluateAll((nodes) =>
    nodes.map((n) => (n as HTMLOptionElement).value).filter(Boolean),
  );
  const other = others.find((value) => value !== first);
  if (other) await later.selectOption(other);
  await expect(page.getByText(/records decided by both runs/)).toBeVisible();
  await page.getByLabel("Confidence move above").selectOption("0.25");
  await expect(page).toHaveURL(/delta=0\.25/);
  await expect(page.getByText(/confidence moved more than 25%/)).toBeVisible();

  // A changed decision opens the evidence behind the later run's answer.
  const changed = page.getByRole("heading", { name: "Decisions that changed" }).locator("xpath=ancestor::section[1]");
  const link = changed.getByRole("link").first();
  if (await link.count()) {
    await link.click();
    await expect(page).toHaveURL(/\/queue\/[0-9a-f-]{36}$/);
    await page.goBack();
  }

  await expectFits(page, "compare", info);
  await page.screenshot({ path: path.join(SHOTS, "compare-runs.png"), fullPage: true });
});
