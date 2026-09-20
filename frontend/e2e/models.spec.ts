/**
 * GATE 9 (feedback loop): the Models page lists the config versions from the
 * database with their lineage, says which one is live, and shows the review
 * rounds if a simulation has been run. Only an admin is offered retune and
 * activate.
 *
 * It never retunes or activates: that would change what every later run
 * scores with, and the API tests already prove both paths. Screenshots land in
 * `reports/e2e/models/` for the README.
 */
import path from "node:path";

import { expect, expectFits, loadState, ROOT, type RunState, signIn, test } from "./support";

test.describe.configure({ mode: "serial" });

let state: RunState;
test.beforeAll(() => {
  state = loadState();
});

const SHOTS = path.join(ROOT, "reports", "e2e", "models");

test("the Models page names the live config and its lineage", async ({ page }, info) => {
  await signIn(page, state.admin);
  await page.getByRole("link", { name: "Models" }).click();
  await expect(page).toHaveURL(/\/models$/);
  await expect(page.getByRole("heading", { name: "Models" })).toBeVisible();

  // The versions table, with exactly one active config.
  const versions = page.getByRole("heading", { name: "Versions" }).locator("xpath=ancestor::section[1]");
  await expect(versions.getByRole("columnheader", { name: "Version" })).toBeVisible();
  await expect(versions.getByText("Active", { exact: true })).toHaveCount(1);
  await expect(page.getByText(/New runs score with /)).toBeVisible();

  // Activation history says who switched it and why.
  const history = page.getByRole("heading", { name: "Activation history" }).locator("xpath=ancestor::section[1]");
  await expect(history.getByRole("listitem").first()).toBeVisible();

  // The rounds chart is there whether or not a simulation has run.
  const rounds = page.getByRole("heading", { name: "Precision and recall by review round" }).locator("xpath=ancestor::section[1]");
  await expect(rounds).toBeVisible();
  const drawn = await rounds.locator("path.recharts-line-curve").count();
  if (drawn > 0) {
    expect(drawn).toBe(3);
    await page.getByRole("button", { name: "Show as table" }).first().click();
    await expect(page.getByRole("columnheader", { name: "Round" })).toBeVisible();
    await page.getByRole("button", { name: "Show as chart" }).first().click();
  } else {
    await expect(rounds.getByText(/No data yet/)).toBeVisible();
  }

  // An admin is offered both, and neither is clicked here.
  await expect(page.getByRole("button", { name: "Retune on labels" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Simulate rounds" })).toBeVisible();

  await expectFits(page, "models", info);
  await page.screenshot({ path: path.join(SHOTS, "models-page.png"), fullPage: true });
  if (drawn > 0) await rounds.screenshot({ path: path.join(SHOTS, "review-rounds.png") });
});

test("an analyst reads the page but is offered nothing that changes a config", async ({ page }) => {
  await signIn(page, state.analyst);
  await page.goto("/models");
  await expect(page.getByRole("heading", { name: "Versions" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retune on labels" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Simulate rounds" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Activate" })).toHaveCount(0);
});
