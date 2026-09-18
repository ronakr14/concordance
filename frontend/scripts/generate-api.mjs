// Regenerate the typed API client from the backend's OpenAPI schema.
//
// Two steps, both reproducible without a running server or a database:
//   1. the backend CLI writes openapi.json from the FastAPI app factory;
//   2. openapi-typescript turns it into src/api/schema.d.ts.
//
// The generated file is committed, so the web app builds without Python. Run
// this (`make client`) whenever an endpoint or schema changes; a stale client
// surfaces as a type error rather than a runtime surprise.

import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const web = resolve(here, "..");
const root = resolve(web, "..");
const schema = join(web, "openapi.json");
const out = join(web, "src", "api", "schema.d.ts");

const venv =
  process.platform === "win32"
    ? join(root, ".venv", "Scripts", "python.exe")
    : join(root, ".venv", "bin", "python");
const python = process.env.PYTHON ?? (existsSync(venv) ? venv : "python");

const run = (cmd, args, cwd) => execFileSync(cmd, args, { cwd, stdio: "inherit" });

run(python, ["-m", "concordance.cli", "api", "openapi", "--out", schema], root);
// `--default-non-nullable false`: a field with a server-side default is
// optional to send. The v7 default types it as required, which would force
// the client to restate every default the API already applies.
run(
  process.execPath,
  [join(web, "node_modules", "openapi-typescript", "bin", "cli.js"), schema, "-o", out, "--default-non-nullable", "false"],
  web,
);
