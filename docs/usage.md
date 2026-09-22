# Usage

How to run Concordance day to day, which database it talks to, and how to log in.
First-time installation is in the README's quick start; this picks up from a
working checkout.

> Every `make <target>` below is `python tasks.py <target>` on a machine without
> `make`, which includes stock Windows.

---

## 1. Start and stop

```
make up              # preflight, migrate, then api + worker + web in this terminal
make up DETACH=1     # the same, in the background
make down            # stop a background start (api drains, worker finishes its job)
make ps              # what is running, with pids and URLs
make logs            # follow a background start's log
```

Ctrl-C stops a foreground `make up` the same way `make down` stops a background
one. If the preflight fails, nothing starts, and each failed check prints what to
do about it.

| What | Where |
|---|---|
| Web app | <http://localhost:5173> |
| API | <http://localhost:8000> |
| API docs (Swagger) | <http://localhost:8000/docs> |

---

## 2. Two databases

The same checkout serves two Neon databases. Nothing is edited to switch between
them: an overlay file is layered over `.env` for one command at a time.

| | Main | Demo |
|---|---|---|
| Database | `neondb` | `concordance_demo` |
| Settings | `.env` | `.env` + `.env.demo` |
| Holds | working data: runs, reviews, assistant history | the fixed-seed demo state |
| Safe to wipe | **no** | yes |

```
make up                                        # main database
CONCORDANCE_ENV_FILE=.env.demo make up         # demo database
```

In PowerShell, set the variable first:

```
$env:CONCORDANCE_ENV_FILE = ".env.demo"; python tasks.py up
Remove-Item Env:CONCORDANCE_ENV_FILE           # back to the main database
```

The overlay works for every command: `make demo`, `make reset-db`, and any
`python -m concordance.cli ...` call. If the named file does not exist the
command refuses to run, so it can never quietly fall back to the main database.
`.env` and `.env.demo` hold credentials and are gitignored. Never commit either.

**Anything destructive goes to the demo database.** `make demo` and
`make reset-db` both empty every table.

---

## 3. Logging in

### Demo database

`make demo` creates two accounts. The passwords are fixed and are **for the demo
database only**:

| Role | Email | Password | Can |
|---|---|---|---|
| admin | `[REDACTED_EMAIL_ADDRESS_5]` | `demo-admin-password` | everything, including approve, activate configs, run the Lab, read the audit log |
| analyst | `[REDACTED_EMAIL_ADDRESS_6]` | `demo-analyst-password` | review the queue, open cases, ask the assistant |

They are also written to `.run/demo.json`, beside every run and record id the demo
walkthrough uses.

### Main database

The main database has no fixed accounts. Its users are whoever registered, and
passwords are stored only as Argon2 hashes, so they cannot be read back. To create
one while `ENV=development`, where registration is open:

```
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "you@example.com", "password": "<12+ characters>", "role": "admin"}'
```

Or use `POST /auth/register` in the Swagger UI at `/docs`. With `ENV=production`,
only an admin who is already logged in can register accounts.

Sessions: the access token lasts 15 minutes and refreshes silently. The refresh
token lasts 14 days and is rotated on every use. Logging out revokes it.

---

## 4. Everyday tasks

| Task | Command |
|---|---|
| Rebuild the demo state from nothing (~85 min over Neon) | `CONCORDANCE_ENV_FILE=.env.demo make demo YES=1` |
| Reconcile the loaded sanction file | `python -m concordance.cli run reconcile` |
| Replay a run and prove it reproduces | `python -m concordance.cli run replay <run_id>` |
| Diff two runs | `python -m concordance.cli run diff <run_a> <run_b>` |
| List and activate scoring configs | `python -m concordance.cli configs list` / `configs activate <version>` |
| Check both LLM providers answer | `python -m concordance.cli llm ping` |
| Check the environment without starting anything | `make preflight` |

Uploading a new sanction file, reviewing the queue, working cases, comparing
runs and the Lab are all done in the web app. `docs/demo_script.md` walks
through each one.

---

## 5. Tests and checks

```
make test-unit       # no database needed
make lint
make typecheck
make perf            # the 50k x 5k engine budget, opt-in, ~2 min
```

The integration tests need a database. Point them at the demo database, never at
the main one. The reset test only runs when `CONCORDANCE_DESTRUCTIVE_TESTS=1` is
set, which CI does and a developer machine should not. CI runs everything on each
push to `main`.

---

## 6. When something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| preflight: port 8000 or 5173 in use | an earlier start is still running | `make down`, or `make ps` to find it |
| preflight: database unreachable | Neon compute idle, or network | retry in a few seconds; `python -m concordance.cli db ping` |
| `failed to resolve host` in a log | this machine's DNS drops lookups intermittently | long runs retry on their own; rerun a short command |
| `CONCORDANCE_ENV_FILE names ... which does not exist` | overlay path typo | check the file name; it is relative to the repo root |
| login rejected on the demo database | demo not built yet | `CONCORDANCE_ENV_FILE=.env.demo make demo YES=1` |
| the assistant says the LLM is disabled | `LLM_ENABLED=false` or no API key in `.env` | set a Groq or OpenRouter key |
