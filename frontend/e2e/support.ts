import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { expect, type Page, test as base, type TestInfo } from "@playwright/test";

export const E2E_DIR = path.dirname(fileURLToPath(import.meta.url));
export const ROOT = path.resolve(E2E_DIR, "..", "..");
const STATE_DIR = path.join(ROOT, "reports", "e2e", "state");
const STATE_FILE = path.join(STATE_DIR, "run.json");

export const PASSWORD = "an end to end passphrase 9";

export type Role = "match" | "ambiguous" | "organization";

export interface RunState {
  tag: string;
  admin: string;
  analyst: string;
  fixture: {
    file: string;
    authority: string;
    headers: Record<string, string>;
    records: { record_id: string; role: Role; name: string }[];
  };
}

export function saveState(state: RunState): void {
  mkdirSync(STATE_DIR, { recursive: true });
  writeFileSync(STATE_FILE, JSON.stringify(state, null, 2));
}

export function loadState(): RunState {
  return JSON.parse(readFileSync(STATE_FILE, "utf-8")) as RunState;
}

export function stateDir(): string {
  mkdirSync(STATE_DIR, { recursive: true });
  return STATE_DIR;
}

/** Run the Python fixture script; its last stdout line is the JSON result. */
export function fixture<T>(...args: string[]): T {
  const venv = path.join(ROOT, ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  const python = existsSync(venv) ? venv : "python";
  const out = execFileSync(python, [path.join(ROOT, "scripts", "e2e_fixture.py"), ...args], { cwd: ROOT, encoding: "utf-8" });
  const last = out.trim().split(/\r?\n/).at(-1) ?? "";
  return JSON.parse(last) as T;
}

/**
 * Every test fails on a console error or an uncaught exception.
 *
 * `allowConsole` lists patterns a test provokes on purpose - the error-state
 * test makes the API fail, and the browser reports that as a console error.
 */
export const test = base.extend<{ allowConsole: RegExp[]; consoleGuard: void }>({
  allowConsole: [[], { option: true }],
  consoleGuard: [
    async ({ page, allowConsole }, use, info) => {
      const problems: string[] = [];
      page.on("console", (message) => {
        if (message.type() !== "error") return;
        const text = `${message.text()} @ ${message.location().url}`;
        if (!allowConsole.some((re) => re.test(text))) problems.push(text);
      });
      page.on("pageerror", (error) => problems.push(`uncaught: ${error.message}`));
      await use();
      if (problems.length) await info.attach("console-errors", { body: problems.join("\n") });
      expect(problems, "console errors during the test").toEqual([]);
    },
    { auto: true },
  ],
});

export { expect };

export async function signIn(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).not.toHaveURL(/\/login/);
}

/** The page body never scrolls sideways at this width; tables scroll in their own box. */
export async function expectFits(page: Page, screen: string, info: TestInfo): Promise<void> {
  // Let lazy chunks and charts settle before measuring.
  await page.waitForLoadState("networkidle");
  const { scroll, client } = await page.evaluate(() => ({
    scroll: document.documentElement.scrollWidth,
    client: document.documentElement.clientWidth,
  }));
  await page.screenshot({ path: info.outputPath(`${screen}.png`), fullPage: true });
  expect(scroll, `${screen}: page is ${scroll}px wide in a ${client}px viewport`).toBeLessThanOrEqual(client);
}
