import { randomBytes } from "node:crypto";
import path from "node:path";

import type { FullConfig } from "@playwright/test";

import { fixture, PASSWORD, type RunState, saveState, stateDir } from "./support";

/** Users and a fresh workbook for this run, all named by one tag so teardown finds them. */
export default async function globalSetup(config: FullConfig): Promise<void> {
  const base = config.projects[0]?.use.baseURL ?? "http://localhost:5173";
  const api = `${base}/api`;

  const health = await fetch(`${api}/health`).catch((cause: unknown) => {
    throw new Error(`The web app is not reachable at ${base} (${String(cause)}). Start it with \`make web\`.`);
  });
  const body = (await health.json().catch(() => ({}))) as { database?: boolean };
  if (!health.ok || !body.database) {
    throw new Error(`The API behind ${api} is not healthy (${health.status}). Start it with \`make api\`, and a worker with \`make worker\`.`);
  }

  const tag = randomBytes(4).toString("hex");
  const email = (role: string) => `${role}-${tag}@e2e.concordance.example.com`;
  for (const role of ["admin", "analyst"]) {
    const response = await fetch(`${api}/auth/register`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email: email(role), password: PASSWORD, role, full_name: `E2E ${role}` }),
    });
    if (response.status !== 201) throw new Error(`registering the ${role}: ${response.status} ${await response.text()}`);
  }

  const workbook = path.join(stateDir(), `sanctions-${tag}.xlsx`);
  const built = fixture<RunState["fixture"]>("build", tag, workbook);
  saveState({ tag, admin: email("admin"), analyst: email("analyst"), fixture: built });
}
