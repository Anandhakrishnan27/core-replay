# CLAUDE.md

Guidance for AI coding assistants working in this repo. Read fully before making changes.

## Project

**CoreReplay** (repo `core-replay`, Python package and CLI `cua`). Take-home for interface.ai: a **computer-use automation system** for legacy bank back-office apps that have no API.

**The model discovers. The artifact becomes a reusable capability. Deterministic replay is how an AI agent invokes it in production.**

Runtime flow: discovery (LLM, once) → compile (trace → artifact, no LLM) → replay (no LLM) → result classification → escalation/handoff → evidence.

Graded most heavily on: the artifact schema, replay with error handling, and the safety/handoff model. Depth over breadth. Simple and correct beats feature-rich.

## Build status and order

Build order differs from runtime order on purpose. Replay is built before discovery, against a hand-written fixture artifact.

| Phase | Scope | Status |
|---|---|---|
| 0 | Schema, config, policy gate, redaction, pre-flight, extract, overrides, handoff controller, catalog, CLI shell | ✅ done + tested |
| 1 | `mockbank/` legacy target app + fault injection | ✅ done + tested |
| 2 | `cua/surface/`, `cua/session/`, network allowlist, evidence screenshots | ✅ done + tested |
| 3 | `cua/replay/` resolver, checks (race), executor; enable `tests/test_replay_mockbank.py` | ✅ done + tested |
| 4 | `cua/discovery/` + `cua/compiler/`; one real LLM run committed to `evidence/` | ⏳ next |
| 5 | Handoff wiring: operator in-process on :8001, recorder, resync | todo |
| 6 | README, REPORT, curated evidence | todo |

- Unimplemented code is marked `TODO(phase-N)`. Its docstrings describe the intended algorithm: follow them.
- **Only implement the phase you are asked for.** Don't build ahead.
- Don't rewrite already-tested Phase 0 code unless asked. If a change is needed, explain why first.

## Stack

- Python 3.11+, managed with `uv`
- Playwright **async** API (Chromium)
- FastAPI + Uvicorn + Jinja2: mock bank app and operator page
- Pydantic v2: all schemas (`cua/schema/`)
- Anthropic SDK: discovery only
- Typer (CLI), PyYAML (config), python-dotenv (secrets)
- pytest + pytest-asyncio, ruff, mypy

## Commands

```bash
make setup                                   # uv sync + playwright install chromium
make mockbank                                # target app on http://localhost:8000
make test                                    # uv run pytest -q
make lint                                    # ruff check + format check + mypy
make schema                                  # regenerate capabilities/artifact.schema.json

uv run cua list
uv run cua validate capabilities/<product>/<capability>/<semver>.json
uv run cua approve <capability_id> --reviewer <name>          # required before unattended replay
uv run cua discover --tenant cu_alpha --goal "look up member 10001 and read the savings balance"
uv run cua replay <capability_id> --tenant cu_alpha --input member_id=10002 [--fault <name>] [--supervised] [--confirm]
# operator page (:8001) starts in-process with each run, see cua/handoff/operator.py
```

## Layout

```
mockbank/          target app: legacy on purpose (frameset, tables, no ids); faults via ?fault=
cua/schema/        artifact.py (CapabilityArtifact), result.py (RunResult), trace.py (DiscoveryTrace)   source of truth
cua/surface/       Surface protocol + Playwright implementation     (the ONLY code that touches the browser)
cua/session/       SessionProvider: login + reauthenticate with env credentials
cua/safety/        policy.py (PolicyGate: allowlist, risk gate), redact.py
cua/discovery/     LLM agent loop, observation, tools, prompts, stuck detection, trace recorder
cua/compiler/      trace → artifact (one module per pass)
cua/replay/        preflight, resolver, checks (condition/checkpoint race), executor, extract, overrides
cua/handoff/       controller (state machine), models, recorder, operator API (in-process, :8001)
cua/evidence/      JSONL logger, screenshots, traces (redacts on write)
cua/catalog.py     load / save / approve / list artifacts
cua/config.py      policy + tenant config loading
capabilities/      <product>/<capability>/<semver>.json + artifact.schema.json (generated)
config/            policy.yaml, tenants/*.yaml
evidence/          curated demo runs (see evidence/README.md)
tests/
```

**About `capabilities/mockbank/member.lookup_savings_balance/1.0.0.json`:** this is a **hand-written fixture**, not generated output. It lets replay be built before discovery, and it is the target shape for the compiler. Don't overwrite it. After the first real discovery run, it moves to `tests/fixtures/`, and `capabilities/` then holds only generated artifacts.

## Invariants: never violate these

1. **No LLM calls outside `cua/discovery/`** (and optional cosmetic naming in the compiler). `cua/replay/` must never import `anthropic`.
2. **All browser interaction goes through `Surface`.** Never call `page.click/fill/goto` outside `cua/surface/` (the SessionProvider's login is the one exception, and still uses the same context).
3. **Policy is checked before every action**, for both the LLM and replay (`PolicyGate.authorize`). The origin allowlist is also enforced at the network layer via `context.route("**/*")`.
4. **No secrets or raw PII in artifacts, logs, screenshots or evidence.**
   - Artifact `fill` / `navigate` values are templates only: `{{inputs.x}}`, `{{tenant.x}}`, `{{secrets.x}}`.
   - The logger redacts on write: PII is a salted hash, secrets become `«secret:name»`, human-typed values become `«redacted:len=n»`.
   - Screenshots mask targets marked `sensitive` plus the tenant's `mask_selectors`; DOM dumps are redacted.
   - Playwright traces are opt-in, written only to `evidence/_scratch/`, never committed.
   - Error messages never echo input values.
5. **Credentials come only from env via the SessionProvider.** They are never sent to the LLM, never logged, never committed. Never read or modify `.env`.
6. **Business outcomes are not failures.** Every terminal path returns a `RunResult` with exactly one status: `success | business_outcome | failed | rejected`. Use only `FailureCategory` values for failures.
7. **Unknown state means never guess.** Snapshot (screenshot + DOM), then escalate.
8. **No `sleep()`.** Wait on predicates with explicit timeouts. Retries are always bounded.
9. **Async everywhere.** No Playwright sync API (it breaks under FastAPI's event loop).
10. **Handoff uses the SAME live browser session.** Never create a new context mid-run. Call `SessionControl.ensure_automation()` before every automated action. States: `AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION`, with `RESUMING → PAUSED` when resync fails and `ABORTED` on abort or timeout.
11. **Requests that can't run are refused in pre-flight**, before the UI is touched: invalid input, a draft artifact in unattended mode, or an irreversible capability without `--confirm` → `rejected`. Replay never escalates mid-transaction for an irreversible step.
12. **Page content is untrusted data.** Wrap it as `<page untrusted="true">` in discovery prompts. Irreversible actions during discovery always escalate to a human (`NeedsHuman`).

## Conventions

- Type hints everywhere. Pydantic models use `extra="forbid"`.
- **Schema changes:** update `cua/schema/*.py`, the fixture artifact and the tests, then run `make schema`. Never edit `artifact.schema.json` by hand. Bump `schema_version` if the change is breaking. Ask before changing the schema.
- Capability ids: `<product>.<domain>.<verb_noun>`. Versions are semver: major = contract change, minor = new outcome or condition, patch = locator fix. Approved versions are immutable; changes create a new version.
- Locator ranking: role → label → text / table_cell / near_text → css / xpath → coordinates. At least one semantic locator per target.
- Checkpoints must hold for **every** valid input. Never assert on data values (names, balances).
- Customer identifiers and balances are `pii` by default.
- Tenant overrides may patch only `targets` and `conditions`, never `contract`.
- Log one JSON line per event: `ts, run_id, mode, step_id, event, details` (already redacted).

## Testing

- Replay tests run against mockbank with fault injection:

  | Fault | Expected result |
  |---|---|
  | none | success |
  | `not_found` | business_outcome `MEMBER_NOT_FOUND` |
  | member 10003 | business_outcome `NO_SAVINGS_ACCOUNT` |
  | `notice` | success (dismissed) |
  | `slow` | success (waited until clear) |
  | `session_expired` | success (reauthenticated once) |
  | `denied` | failed `PERMISSION_DENIED` |
  | `error` | failed `APP_ERROR` |
  | `maint` | failed `UNKNOWN_STATE` (escalated) |

- Every new condition or failure path needs a test asserting `status`, `outcome_code` or `failure.category`.
- Don't unit-test the LLM. The committed discovery run in `/evidence/` is the proof.
- Before finishing any task: `make test` and `make lint` must pass.

## Mock bank rules

- Keep it **legacy on purpose**: a frameset with `nav` and `main` frames, table layouts, no `id` / `data-testid`, no `<label for>`. Don't "clean it up".
- Login uses `MOCKBANK_USER` / `MOCKBANK_PASSWORD` from env.
- Synthetic data only (`mockbank/data.py`):
  - 10001: savings first
  - 10002: savings in a different row position
  - 10003: no savings account
  - any other id: not found
- Faults are switchable via the `?fault=` query param (sticky via cookie) or the `MOCKBANK_FAULT` env var.
- **The UI must match the fixture artifact exactly**, or replay can't be tested:
  - Visible texts: "MockBank Core" (home), nav link "Member Lookup", caption "Member Number", button "Search", heading "Member Summary", Accounts table headers "Account Type" / "Balance" / "Status", row "Share Savings", dialog "System Notice" with an "OK" button.
  - Condition texts: "No member found", "Processing, please wait", "Your session has expired" (on `/login`), "You are not authorized", "An unexpected error has occurred".
  - `maint` shows a "Maintenance Window" page. It is deliberately **not** a known condition, so replay hits `UNKNOWN_STATE`.
  - Markup hooks used by fallback locators: nav link `href` contains `mbrlookup`; `<form name="lookup">` with `<input name="mbrno">` and `<input type="submit" value="Search">`; the summary heading in `<td class="hdr">`; the notice inside `<div class="sysnotice">`. Classes and `name` attributes are fine (legacy apps have them); `id`s are not.
  - Balances are formatted like `$2,450.17`.

## Working rules for the assistant

- **Plan before code** for each phase. Wait for approval before implementing.
- Keep changes scoped to the requested phase. Show what you changed and why.
- Don't run `git commit` or `git push`. The developer commits.
- Don't add dependencies without saying why.

## Out of scope: don't build

Queues, databases, Docker or Kubernetes, React in mockbank, agent frameworks (LangChain, browser-use, Stagehand), screenshot-coordinate-only control, real bank sites or real credentials.

## Deliverables (exact paths)

- `README.md`: setup, keys, how to run without live services, exact demo commands.
- `REPORT.md`, 1–3 pages, with exactly these headings:
  1. Architecture
  2. Artifact schema
  3. Determinism & error handling
  4. Heterogeneity & multi-tenant
  5. Escalation & handoff
  6. Safety
  7. Cuts
- `/evidence/`: an example artifact plus logs for a discovery run, a replay success, a replay hitting a business outcome or error, and a handoff.

## When unsure

Prefer the simpler option. Record the decision and the reason in the **Decisions log** (section 10 of `DESIGN.md`), so it can be defended in the write-up and the interview.