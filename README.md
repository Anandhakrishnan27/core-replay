# CoreReplay

**Reliable AI-agent capabilities for legacy banking back-office apps that have no API.**

> **The model discovers. The artifact becomes a reusable capability. Deterministic replay is how an AI agent invokes it in production.**

Banks and credit unions run many back-office apps (core banking screens, servicing tools, admin consoles)
whose only interface is the UI a teller uses. CoreReplay lets an AI agent work those apps safely:

1. **Discover (LLM, once).** Given a goal such as *"look up member 10001 and read the savings balance"*, an
   LLM drives the live app through its accessibility tree until the goal is met. Every action is
   policy-checked, and every value it sees or types is redacted.
2. **Compile (no LLM).** The run becomes a typed, versioned **capability artifact**: a contract (inputs,
   outputs, declared business outcomes) plus an implementation (ranked semantic locators, checkpoints,
   known runtime conditions). It is plain JSON, reviewable in a pull request, and holds no PII or
   credentials.
3. **Replay (no LLM).** An agent invokes the capability by id with typed inputs. Replay is deterministic:
   it finds each control by stable locators, waits on predicates (never sleeps), and races every step
   against the known conditions. It returns exactly one of `success`, `business_outcome`, `failed` or
   `rejected`.
4. **Hand off (human in the loop).** When replay or discovery reaches a screen it can't classify, it pauses
   on the **same live browser session** and raises an intervention request. A human takes control, fixes
   the screen, and hands back. Replay then verifies where it is against its checkpoints and resumes. The
   human's actions are recorded, redacted.

The target here is **MockBank**, a deliberately legacy web app included in the repo: a frameset, table
layouts, no ids and no `<label for>`, with synthetic members and switchable runtime faults.

```text
goal ─► DISCOVERY (LLM, once) ─► COMPILE (no LLM) ─► capability artifact (JSON, versioned, reviewed)
                                                              │
        caller inputs ─────────────────────────────► REPLAY (no LLM) ─► success | business_outcome | failed | rejected
                                                              │
                                     stuck / unknown state ─► HUMAN HANDOFF (same live session) ─► resync ─► resume
```

- **Design write-up:** [`REPORT.md`](REPORT.md) (architecture, artifact schema, error handling, multi-tenant, handoff, safety, cuts)
- **Recorded runs:** [`evidence/`](evidence/) (real LLM discovery, replays, a real human handoff)
- **Example artifact:** [`capabilities/mockbank/member.lookup_savings_balance/1.1.0.json`](capabilities/mockbank/member.lookup_savings_balance/1.1.0.json),
  compiled from the real discovery run in [`evidence/discovery/`](evidence/discovery/)

The Python package and CLI are called `cua` (computer-use automation).

## Setup

Requirements: Python 3.11+ and [uv](https://docs.astral.sh/uv/). The commands below are for macOS or Linux.

```bash
make setup                 # uv sync + playwright install chromium
```

Then create a `.env` file in the repo root (it is git-ignored). Both the `cua` CLI and the mock bank read it:

```bash
# Any values: the mock bank accepts exactly these credentials
MOCKBANK_USER=teller
MOCKBANK_PASSWORD=change-me
# A long random string
CUA_REDACTION_SALT=replace-with-a-long-random-string
# Discovery only
ANTHROPIC_API_KEY=your-key
```

| Variable | Needed for | Notes |
| --- | --- | --- |
| `MOCKBANK_USER`, `MOCKBANK_PASSWORD` | every run | Choose any values; there is no default. The mock bank accepts exactly these, and the session provider signs in with them. Never logged and never sent to the LLM. |
| `CUA_REDACTION_SALT` | every run | Salt for hashing PII in logs and evidence. Set a long random value. |
| `ANTHROPIC_API_KEY` | discovery only | Replay never calls an LLM. |
| `CUA_MODEL` | discovery, optional | Defaults to `claude-opus-5-5`. |
| `CUA_HEADLESS` | optional | Hides the browser. A human can only take over a visible one. |

## Demo

Start the target app in one terminal and leave it running:

```bash
make mockbank              # MockBank on http://localhost:8000
```

Then, in a second terminal:

```bash
make demo-discover         # real LLM run: goal → trace → compiled artifact → self-test replay → saved as 1.2.0
make demo-replay           # replay with a member discovery never saw (10002) → success + savings_balance
make demo-notfound         # member 99999 → business_outcome MEMBER_NOT_FOUND (a result, not an error)
make demo-handoff          # injected "Maintenance Window" → escalation → human takeover → success
```

**No API key?** Skip `demo-discover`. The replay demos run the newest artifact in the catalog: the
committed `1.1.0`, from a real discovery run, until you discover a newer one. Replay never needs a key.
Saved versions are immutable, so `demo-discover` saves a new version (`1.2.0` by default; pass
`VERSION=x.y.z` for another run).

**The handoff demo**, step by step:

1. Run `make demo-handoff` and open the operator URL it prints (`http://127.0.0.1:8001/?token=…`).
2. A Chromium window opens and reaches a *Maintenance Window* page, which no artifact knows.
3. After the step's 10 s timeout the run escalates. The operator page shows **PAUSED**, with the reason
   and a masked screenshot. Until then the browser ignores your clicks: automation holds control.
4. Click **Take control**, click **Try Again** in Chromium, then click **Hand back**.
5. Replay checks which checkpoint now holds, resumes after it, and returns `success`. The result lists
   your redacted actions under `handoffs[].human_actions`.

**Unattended replay** (the production path) needs an approved artifact:

```bash
uv run cua approve mockbank.member.lookup_savings_balance --version 1.1.0 --reviewer <name>
uv run cua replay  mockbank.member.lookup_savings_balance --version 1.1.0 --input member_id=10002
```

`cua approve` records the reviewer in the artifact's `review` block, rewriting that file. Without approval,
an unattended run is `rejected` in pre-flight, before the browser opens. The demos pass `--supervised`,
which allows a draft artifact.

## CLI

| Command | What it does | Exit codes |
| --- | --- | --- |
| `cua discover --goal … --capability-id … -p name=value [--version x.y.z]` | Real LLM run → compile → self-test replay → save to the catalog (only if the self-test passed) | 0 saved, 1 failed, 2 refused |
| `cua replay <capability_id> -i name=value [--version] [--supervised] [--confirm] [--fault]` | Deterministic replay; prints the `RunResult` JSON | 0 success or business outcome, 1 failed, 2 rejected |
| `cua validate <file>` | Validate an artifact file (schema + cross-references) | 0 valid, 1 invalid |
| `cua approve <capability_id> --reviewer <name> [--version]` | Mark an artifact approved (required for unattended replay) | |
| `cua list` | List the capability catalog | |

Useful options:

- `--tenant` (default `cu_alpha`): which tenant config to use.
- `--operator / --no-operator`: attach the operator page (default: on for `discover` and `--supervised`).
- `--operator-port` (default 8001).
- `--evidence-dir`: where the run's evidence folder is written (default `evidence/_scratch/`, which is git-ignored).
- `--confirm`: required for any capability containing an irreversible step.

## Runtime faults

Add `--fault <name>` to `cua replay` (or `cua discover`) to make MockBank misbehave:

| Fault / input | What MockBank does | Replay result |
| --- | --- | --- |
| *(none)*, member `10002` | normal | `success` |
| member `99999` or `--fault not_found` | "No member found" | `business_outcome` `MEMBER_NOT_FOUND` |
| member `10003` | member has no savings account | `business_outcome` `NO_SHARE_SAVINGS` |
| `notice` | "System Notice" dialog | recovered (dismissed) → `success` |
| `slow` | "Processing, please wait" page | recovered (waited until clear) → `success` |
| `session_expired` | bounced to the login page | recovered (re-authenticated once) → `success` |
| `denied` | "You are not authorized" | `failed` `PERMISSION_DENIED` |
| `error` | "An unexpected error has occurred" | `failed` `APP_ERROR` |
| `maint` | unknown "Maintenance Window" page | `failed` `UNKNOWN_STATE` unattended; human handoff → `success` with an operator |

## Running without live services

- **Replay needs no API key:** a committed artifact plus the local MockBank are enough.
- **`make test` runs the whole suite offline:** MockBank is started in-process, the LLM is stubbed, and a local Chromium is used. It takes about 4 minutes.
- **Discovery is the only step that calls a model.** Its real runs are recorded in [`evidence/discovery/`](evidence/discovery/).

## Evidence

[`evidence/`](evidence/) holds curated runs, grouped by kind. Each run folder has a `run.jsonl` event log,
masked step screenshots and redacted DOM dumps on failure. Replays also have `result.json`; discovery runs
have `trace.json`, the compiled `artifact.json` and the self-test. See
[`evidence/README.md`](evidence/README.md) for a run-by-run index.

| Folder | Shows |
| --- | --- |
| `discovery/` | The real LLM discovery run: goal → trace → compiled artifact → self-test → saved as `1.1.0` |
| `replay_success/` | Replay with a new input → `success` |
| `business_outcome/` | `MEMBER_NOT_FOUND`, `NO_SHARE_SAVINGS` |
| `recovery/` | Notice dismissed; session re-authenticated |
| `hard_failure/` | `PERMISSION_DENIED`, with redacted DOM per frame |
| `human_handoff/` | A real human takeover of the live session, then resync → `success` |

No raw member numbers, names, SSNs, balances or account numbers appear in any log, result or DOM dump.
Screenshots mask PII, with bank-style partial values in evidence only (e.g. SSN `•••-••-7731`).

## Project layout

```text
cua/
  cli.py               the `cua` command
  schema/              artifact.py (capability artifact), result.py (RunResult), trace.py (discovery trace)
  discovery/           LLM agent loop, tools, prompts, stuck detection, trace recorder, discover pipeline
  compiler/            trace → artifact, one module per pass, + self-test replay
  replay/              pre-flight, executor (condition/checkpoint race, recoveries, handoff), extract, overrides
  handoff/             control state machine, operator page/API, human-action recorder, browser input lock
  surface/             the only code that touches the browser: Playwright surface, locators, selector engines
  session/             sign-on and re-authentication (credentials from env only)
  safety/              policy gate (allowlists, risk), redaction
  evidence/            JSONL run logger
mockbank/              the target app (FastAPI + Jinja2, legacy on purpose) and fault injection
capabilities/          the catalog: <product>/<capability>/<semver>.json, plus the generated artifact.schema.json
config/                policy.yaml, tenants/*.yaml, products/*.conditions.yaml (vendor condition packs)
evidence/              curated runs (see above)
tests/                 unit and end-to-end tests against MockBank
```

[`DESIGN.md`](DESIGN.md) holds the working design notes and a dated log of every non-obvious decision.

## Development

```bash
make test      # full test suite (offline)
make lint      # ruff + format check + mypy
make schema    # regenerate capabilities/artifact.schema.json from the Pydantic models
```
