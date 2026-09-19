/**
 * GATE 9 (Lab): the page renders the robustness curve and the reliability
 * diagram from the real sweep in the database, the dial and the toggles drive
 * every panel, and only an admin is offered the buttons that start work.
 *
 * Reads whatever sweep is newest; it never starts one - a sweep is minutes of
 * worker CPU and the API tests already prove the request path. Screenshots land
 * in `reports/e2e/lab/` for the README.
 */
import path from "node:path";

import { expect, expectFits, loadState, ROOT, type RunState, signIn, test } from "./support";

test.describe.configure({ mode: "serial" });

let state: RunState;
test.beforeAll(() => {
  state = loadState();
});

const SHOTS = path.join(ROOT, "reports", "e2e", "lab");

test("the Lab draws the sweep and the dial drives every panel", async ({ page }, info) => {
  await signIn(page, state.admin);
  await page.getByRole("link", { name: "Lab" }).click();
  await expect(page).toHaveURL(/\/lab$/);
  await expect(page.getByRole("heading", { name: "Lab" })).toBeVisible();

  const curve = page.getByRole("img", { name: /Line chart of F1 by corruption level/ });
  await expect(curve).toBeVisible();
  // Three sweep strategies, one path each, drawn from real cells.
  await expect(curve.locator("path.recharts-line-curve")).toHaveCount(3);
  const reliability = page.getByRole("img", { name: /observed match rate against predicted confidence/ });
  await expect(reliability.locator(".recharts-scatter")).toHaveCount(2);
  await expect(page.getByText(/Before calibration · ECE \d\.\d{3}/)).toBeVisible();
  await expect(page.getByText(/After isotonic · ECE \d\.\d{3}/)).toBeVisible();

  // The dial: 50% by default, moved to 90%, and every panel follows it.
  await expect(page.getByRole("heading", { name: "At 50% corruption" })).toBeVisible();
  await page.getByRole("slider", { name: "Corruption" }).fill("0.9");
  await expect(page).toHaveURL(/level=0\.9/);
  await expect(page.getByRole("heading", { name: "At 90% corruption" })).toBeVisible();
  await expect(page.getByText(/model at 90% corruption, on the fit's holdout/)).toBeVisible();
  await expect(page.getByText(/decided correctly at 90% corruption/)).toBeVisible();

  // Clicking the curve moves the dial too.
  const box = await curve.boundingBox();
  if (!box) throw new Error("the robustness curve has no box");
  await page.mouse.click(box.x + 60, box.y + box.height / 2);
  await expect(page).not.toHaveURL(/level=0\.9/);

  // A strategy toggled off leaves the chart, keeps its colour for the others, and comes back.
  await page.getByRole("button", { name: "Fuzzy", exact: true }).click();
  await expect(page.getByRole("button", { name: "Fuzzy", exact: true })).toHaveAttribute("aria-pressed", "false");
  await expect(curve.locator("path.recharts-line-curve")).toHaveCount(2);
  await page.getByRole("button", { name: "Fuzzy", exact: true }).click();
  await expect(curve.locator("path.recharts-line-curve")).toHaveCount(3);

  // Every chart has its numbers as a table.
  await page.getByRole("radio", { name: "Recall" }).click();
  await expect(page.getByRole("img", { name: /Line chart of Recall/ })).toBeVisible();
  await page.getByRole("button", { name: "Show as table" }).first().click();
  await expect(page.getByRole("columnheader", { name: "Probabilistic", exact: true }).first()).toBeVisible();
  await page.getByRole("button", { name: "Show as chart" }).first().click();

  // The scenario breakdown names capabilities, not just an aggregate.
  await expect(page.getByRole("rowheader", { name: "Missing npi" })).toBeVisible();

  // The cost panel shows a measured sample, or says plainly why there is none.
  const cost = page.getByRole("heading", { name: "LLM cost versus an LLM-on-everything baseline" }).locator("xpath=ancestor::section[1]");
  await expect(cost.getByText(/fewer model calls|No LLM sample yet|LLM sample failed/)).toBeVisible();

  await expect(page.getByRole("button", { name: "Run sweep" })).toBeVisible();

  await page.getByRole("slider", { name: "Corruption" }).fill("0.5");
  await page.getByRole("radio", { name: "F1" }).click();
  await expect(page.getByRole("heading", { name: "At 50% corruption" })).toBeVisible();
  await expectFits(page, "lab", info);
  await page.screenshot({ path: path.join(SHOTS, "lab-page.png"), fullPage: true });
  await curve.screenshot({ path: path.join(SHOTS, "robustness-curve.png") });
  await reliability.locator("xpath=ancestor::section[1]").screenshot({ path: path.join(SHOTS, "reliability-diagram.png") });
  await cost.screenshot({ path: path.join(SHOTS, "llm-cost.png") });
});

test("an analyst reads the Lab but is offered nothing that starts work", async ({ page }) => {
  await signIn(page, state.analyst);
  await page.goto("/lab");
  await expect(page.getByRole("img", { name: /Line chart of F1 by corruption level/ })).toBeVisible();
  await expect(page.getByRole("button", { name: "Run sweep" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Run LLM sample" })).toHaveCount(0);
});
