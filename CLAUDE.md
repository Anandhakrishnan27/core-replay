# CLAUDE.md

Guidance for AI coding assistants working in this repo. Read fully before making changes.

## Project

**CoreReplay** (repo `core-replay`, Python package and CLI `cua`). Take-home for interface.ai: a **computer-use automation system** for legacy bank back-office apps that have no API.

**The model discovers. The artifact becomes a reusable capability. Deterministic replay is how an AI agent invokes it in production.**

Flow: discovery (LLM, once) → compile (trace → artifact, no LLM) → replay (no LLM) → result classification → escalation/handoff → evidence.

Graded most heavily on: the artifact schema, replay with error handling, and the safety/handoff model. Depth over breadth. Simple and correct beats feature-rich.

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
uv sync && uv run playwright install chromium
uv run uvicorn mockbank.app:app --port 8000          # target app
# operator page (:8001) starts in-process with each run, see cua/handoff/operator.py
uv run cua discover --tenant cu_alpha --goal "look up member 10001 and read the savings balance"
uv run cua validate capabilities/<path>.json
uv run cua replay <capability_id> --tenant cu_alpha --input member_id=10001 [--fault <name>]
uv run pytest
uv run ruff check . && uv run ruff format .
```

## Layout

```
mockbank/          target app — legacy on purpose (frameset, tables, no ids); faults via ?fault=
cua/schema/        CapabilityArtifact, RunResult, FailureCategory   (source of truth)
cua/surface/       Surface protocol + Playwright implementation     (the ONLY code that touches the browser)
cua/session/       session provider: login with env credentials, re-auth
cua/safety/        policy (allowlist, risk gate), redaction
cua/discovery/     LLM agent loop, observation, tools, stuck detection, trace recorder
cua/compiler/      trace → artifact
cua/replay/        executor, locator resolver, checks/condition race
cua/handoff/       control lease/state machine, recorder, operator API (in-process, :8001)
cua/catalog.py     load / save / approve / list artifacts
cua/config.py      policy + tenant config loading
cua/evidence/      JSONL logger, screenshots, traces (redacts on write)
capabilities/      saved artifacts: <product>/<capability>/<semver>.json + artifact.schema.json
config/            policy.yaml, tenants/*.yaml
evidence/          committed demo runs (discovery, replay success, not-found, handoff)
tests/
```

## Invariants: never violate these

1. **No LLM calls outside `cua/discovery/`** (and optional cosmetic naming in the compiler). `cua/replay/` must never import `anthropic`.
2. **All browser interaction goes through `Surface`.** Never call `page.click/fill/goto` outside `cua/surface/`.
3. **Policy is checked before every action**, for both the LLM and replay. The origin allowlist is also enforced at the network layer via `context.route("**/*")`.
4. **No secrets or raw PII in artifacts, logs, screenshots or evidence.**
   - Artifact `fill` values are templates only: `{{inputs.x}}`, `{{tenant.x}}`, `{{secrets.x}}`.
   - The logger redacts on write: PII is hashed, secrets become `«secret:name»`.
   - Screenshots mask targets marked `sensitive`.
5. **Credentials come only from env via the session provider.** They are never sent to the LLM, never logged, never committed. `.env` is git-ignored.
6. **Business outcomes are not failures.** Every terminal path returns a `RunResult` with exactly one status: `success | business_outcome | failed | rejected`. Use only `FailureCategory` values for failures.
7. **Unknown state means never guess.** Snapshot (screenshot + DOM), then escalate.
8. **No `sleep()`.** Wait on predicates with explicit timeouts. Retries are always bounded.
9. **Async everywhere.** No Playwright sync API (it breaks under FastAPI's event loop).
10. **Handoff uses the SAME live browser session.** Never create a new context mid-run. Check the control lease before every automated action.
11. **Requests that can't run are refused in pre-flight**, before the UI is touched: invalid input, a draft artifact in unattended mode, or an irreversible step without confirmation → `rejected`.
12. **Page content is untrusted data.** Wrap it as such in discovery prompts. Irreversible actions during discovery always escalate to a human.

## Conventions

- Type hints everywhere. Pydantic models use `extra="forbid"`.
- Schema change checklist: update the models, the example artifact and the tests, and regenerate `capabilities/artifact.schema.json`. Bump `schema_version` if the change is breaking.
- Capability ids: `<product>.<domain>.<verb_noun>`. Versions are semver: major = contract change, minor = new outcome or condition, patch = locator fix.
- Locator ranking: role → label → text / table_cell / near_text → css / xpath → coordinates. At least one semantic locator per target.
- Checkpoints must hold for **every** valid input. Never assert on data values (names, balances).
- Log one JSON line per event: `ts, run_id, mode, step_id, event, details` (redacted).

## Testing

- Replay tests run against mockbank with fault injection: success, not_found, notice (dismissed), slow (retried), session_expired, denied, error, maint (unknown state).
- Every new condition or failure path needs a test asserting `status`, `outcome_code` or `failure.category`.
- Don't unit-test the LLM. The committed discovery run in `/evidence/` is the proof.

## Mock bank rules

- Keep it **legacy on purpose**: framesets, table layouts, no `id`/`data-testid`, no `<label for>`. Don't "clean it up".
- Synthetic data only. Faults are switchable via the `?fault=` query param or the `MOCKBANK_FAULT` env var.

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

Prefer the simpler option. Record the decision and the reason in the "Decisions" section of `DESIGN.md`, so it can be defended in the write-up and the interview.
