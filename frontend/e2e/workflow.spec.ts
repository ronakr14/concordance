/**
 * GATE 8: the whole workflow, driven in a browser.
 *
 * login -> upload -> map columns -> commit -> run -> review -> approve -> case
 * -> audit; an organization record reviewed with its own field set; an analyst
 * never offered an admin action; the loading, empty and error states; and
 * every screen at 1280px. Serial: each step builds on the one before.
 */
import type { Page } from "@playwright/test";

import { expect, expectFits, loadState, type Role, type RunState, signIn, test } from "./support";

test.describe.configure({ mode: "serial" });

// Read when the tests run, not when the file is collected: global setup writes it.
let state: RunState;
test.beforeAll(() => {
  state = loadState();
});
const record = (role: Role, n = 0) => state.fixture.records.filter((r) => r.role === role)[n]!;

/** Header -> canonical field, as the analyst maps it. `Town` has no canonical field: kept raw. */
const CANONICAL: Record<string, string> = { dob_raw: "dob", city: "" };
const mapping = () => Object.entries(state.fixture.headers).map(([field, header]) => [header, CANONICAL[field] ?? field] as const);

let queueUrl = "";
let caseUrl = "";

async function openRecord(page: Page, recordId: string): Promise<void> {
  await page.goto(queueUrl);
  await page.getByRole("row").filter({ hasText: recordId }).click();
  await expect(page.getByText(`Record ${recordId}`, { exact: false })).toBeVisible();
}

test.describe("sign-in", () => {
  // The refusal is the point of the test; the browser logs the login's 401.
  test.use({ allowConsole: [/status of 401 .*\/api\/auth\/login$/] });

  test("refuses bad credentials with one message", async ({ page }) => {
    await page.goto("/queue");
    await expect(page).toHaveURL(/\/login\?next=%2Fqueue/);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByText("Enter an email address.")).toBeVisible();

    await page.getByLabel("Email").fill(state.admin);
    await page.getByLabel("Password").fill("not the password at all");
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page.getByRole("alert")).toHaveText(/Email or password is incorrect\./);
  });
});

test("admin uploads, maps columns, commits and reconciles", async ({ page }, info) => {
  await signIn(page, state.admin);
  await page.goto("/sanctions/upload");
  await page.getByLabel("Sanction workbook").setInputFiles(state.fixture.file);
  await page.getByLabel("Source authority (optional)").fill(state.fixture.authority);
  await page.getByRole("button", { name: "Upload and inspect" }).click();

  // Step 2: the mapping. Take the name columns away first - commit must refuse.
  await expect(page.getByRole("table", { name: "Map each detected column to a canonical field" })).toBeVisible();
  for (const header of [state.fixture.headers.last_name, state.fixture.headers.organization_name]) {
    await page.getByLabel(`Canonical field for ${header}`, { exact: true }).selectOption("");
  }
  await expect(page.getByRole("alert")).toContainText("Every record needs a name to be matched on");
  const commit = page.getByRole("button", { name: /^Commit \d+ rows$/ });
  await expect(commit).toBeDisabled();

  for (const [header, field] of mapping()) await page.getByLabel(`Canonical field for ${header}`, { exact: true }).selectOption(field);
  await expect(page.getByRole("alert")).toHaveCount(0);
  // The preview reads the samples through the mapping as it stands.
  await expect(page.getByRole("columnheader", { name: "Last name" })).toBeVisible();
  await expect(page.getByRole("cell", { name: record("match").name, exact: true }).first()).toBeVisible();
  await expectFits(page, "upload-mapping", info);

  await expect(commit).toHaveText(`Commit ${state.fixture.records.length} rows`);
  await commit.click();
  await expect(page.getByRole("heading", { name: "Ingested" })).toBeVisible();
  const newRecords = page.getByText("New records", { exact: true }).locator("..");
  await expect(newRecords).toContainText(String(state.fixture.records.length));

  // Step 3: reconcile, watching the run the button started.
  await page.getByRole("button", { name: "Start reconciliation" }).click();
  const review = page.getByRole("button", { name: /Review the results/ });
  await expect(review).toBeVisible({ timeout: 10 * 60_000 });
  await expectFits(page, "upload-done", info);
  await review.click();
  await expect(page).toHaveURL(/run_id=/);
  queueUrl = page.url().replace(new URL(page.url()).origin, "");
  await expect(page.getByRole("row").filter({ hasText: state.fixture.authority })).toHaveCount(state.fixture.records.length);
});

test("admin approves a match; the case opens and shows in the audit log", async ({ page }, info) => {
  await signIn(page, state.admin);
  await openRecord(page, record("match").record_id);
  await expect(page.getByRole("heading", { name: "Sanction record" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Evidence, field by field" })).toBeVisible();
  await expectFits(page, "investigation", info);

  await page.getByRole("button", { name: /Approve…/ }).click();
  const dialog = page.getByRole("dialog", { name: "Approve and open a case" });
  await expect(dialog.getByLabel("Case duration (months)")).toHaveValue("3");
  await dialog.getByLabel("Comment (optional)").fill("NPI and date of birth agree");
  await dialog.getByRole("button", { name: "Approve and open case" }).click();
  await expect(page.getByText(/Approved\. Case .+ opened\./)).toBeVisible();
  await page.getByRole("button", { name: "Open case" }).click();

  await expect(page).toHaveURL(/\/cases\/[0-9a-f-]{36}$/);
  caseUrl = new URL(page.url()).pathname;
  await expect(page.getByText("Active", { exact: true }).first()).toBeVisible();
  await expectFits(page, "case-detail", info);

  await page.getByRole("button", { name: "In the audit log" }).click();
  await expect(page).toHaveURL(/\/audit\?entity_type=case/);
  await expect(page.locator("span").filter({ hasText: /^Case opened$/ })).toBeVisible();
  await expectFits(page, "audit", info);
});

test("an organization record is reviewed with the organization field set", async ({ page }) => {
  const org = record("organization");
  await signIn(page, state.admin);
  await openRecord(page, org.record_id);
  await expect(page.getByRole("heading", { level: 1 })).toContainText(org.name);

  const card = page.locator("section").filter({ has: page.getByRole("heading", { name: "Sanction record" }) });
  for (const label of ["Legal name", "DBA", "EIN"]) await expect(card.getByRole("term").filter({ hasText: new RegExp(`^${label}$`) })).toBeVisible();
  for (const label of ["Date of birth", "First name"]) await expect(card.getByRole("term").filter({ hasText: new RegExp(`^${label}$`) })).toHaveCount(0);

  await page.getByRole("button", { name: /Reject…/ }).click();
  const dialog = page.getByRole("dialog", { name: "Reject this match" });
  await dialog.getByLabel("Comment").fill("Different EIN; same trading name only");
  await dialog.getByRole("button", { name: "Reject", exact: true }).click();
  await expect(page.getByText("Rejected", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("“Different EIN; same trading name only”")).toBeVisible();
});

test("an analyst is never offered an admin action", async ({ page }) => {
  await signIn(page, state.analyst);
  const nav = page.getByRole("navigation", { name: "Main" });
  await expect(nav.getByRole("link", { name: "Queue" })).toBeVisible();
  await expect(nav.getByRole("link", { name: "Audit" })).toHaveCount(0);

  await page.goto("/audit");
  await expect(page).toHaveURL(/\/$/);

  await openRecord(page, record("match", 1).record_id);
  await expect(page.getByRole("button", { name: /Reject…/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Escalate…/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Approve…/ })).toHaveCount(0);
  await expect(page.getByRole("link", { name: "Audit trail" })).toHaveCount(0);
  await page.keyboard.press("a"); // the approve shortcut does nothing for an analyst
  await expect(page.getByRole("dialog")).toHaveCount(0);

  await page.getByRole("button", { name: /Escalate…/ }).click();
  const dialog = page.getByRole("dialog", { name: "Escalate to an admin" });
  await dialog.getByLabel("Comment").fill("Two plausible providers in the same practice");
  await dialog.getByRole("button", { name: "Escalate", exact: true }).click();
  await expect(page.getByText("Escalated to an admin, who decides it now.")).toBeVisible();

  await page.goto(caseUrl);
  await expect(page.getByText("Active", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("button", { name: /Close case…/ })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "In the audit log" })).toHaveCount(0);
});

test("every main screen fits 1280px", async ({ page }, info) => {
  await signIn(page, state.admin);
  for (const [screen, url] of [
    ["dashboard", "/"],
    ["providers", "/providers"],
    ["sanctions", "/sanctions"],
    ["queue", queueUrl],
    ["cases", "/cases"],
  ] as const) {
    await page.goto(url);
    await expect(page.getByRole("heading", { level: 1 }).first()).toBeVisible();
    await expectFits(page, screen, info);
  }
  await page.goto("/providers");
  await page.getByRole("row").filter({ hasText: /P\d{7}/ }).first().click();
  await expect(page).toHaveURL(/\/providers\/.+/);
  await expectFits(page, "provider-profile", info);
});

test.describe("states", () => {
  // The error test makes the API fail on purpose; the browser logs that 500.
  test.use({ allowConsole: [/status of 500/] });

  test("loading, empty and error states render", async ({ page }) => {
    await signIn(page, state.admin);

    // Loading: skeletons while the queue request is slow, then the rows.
    await page.route("**/api/matches?**", async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 2_000));
      await route.continue();
    });
    await page.goto("/queue");
    const table = page.getByRole("table", { name: "Review queue" });
    await expect(table).toHaveAttribute("aria-busy", "true");
    await expect(table).not.toHaveAttribute("aria-busy", "true");
    await page.unroute("**/api/matches?**");

    // Empty: a filter nothing matches.
    await page.goto(`/queue?review_status=&run_id=00000000-0000-0000-0000-000000000000`);
    await expect(page.getByText("Nothing matches these filters")).toBeVisible();

    // Error: the dashboard's numbers fail; the card says so and offers a retry.
    await page.route("**/api/stats/kpis", (route) => route.fulfill({ status: 500, json: { error: { code: "internal", message: "boom" } } }));
    await page.goto("/");
    const failed = page.getByRole("alert").filter({ has: page.getByRole("button", { name: "Try again" }) });
    await expect(failed.first()).toBeVisible();
  });
});
