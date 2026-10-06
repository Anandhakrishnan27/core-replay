# CoreReplay: design notes

Working notes for the build. Much of this becomes `REPORT.md`. Keep this file in sync with the code.
When they disagree, the code and `CLAUDE.md` win, so fix this file.

---

## 1. The whole system on one page

```
                 goal + tenant
                      │
      ┌───────────────▼────────────────┐
      │  DISCOVERY  (LLM in the loop)  │  observe → decide → act, bounded steps/time
      │  cua/discovery/                │  stuck → handoff → resume discovery
      └───────────────┬────────────────┘
                      │ trace.json (actions + element snapshots; secrets never present)
      ┌───────────────▼────────────────┐
      │  COMPILER  (no LLM required)   │  clean, parameterize, ranked locators,
      │  cua/compiler/                 │  checkpoints, conditions, risk, validate + self-test
      └───────────────┬────────────────┘
                      │ CapabilityArtifact x.y.z.json (draft) ──► human review ──► approved
      ┌───────────────▼────────────────┐
      │  REPLAY  (no LLM)              │  pre-flight → fingerprint → steps → condition/checkpoint race
      │  cua/replay/                   │  → RunResult {success | business_outcome | failed | rejected}
      └───────────────┬────────────────┘
                      │ unknown state / escalate handler / recovery exhausted
      ┌───────────────▼────────────────┐
      │  HANDOFF                       │  pause → intervention request → human drives the SAME
      │  cua/handoff/ (operator :8001) │  browser → actions recorded → hand back → resync → resume
      └────────────────────────────────┘

 Everything touches the app ONLY through Surface (the seam). Surface runs every action through
 PolicyGate (allowlist + risk gate) and writes only redacted evidence.
 Login happens in the SessionProvider, so neither the LLM nor the artifact ever sees credentials.
```

**The main decision:** the LLM and replay act through the same `Surface` interface. Guardrails, redaction and evidence are enforced there, at one chokepoint, so neither path can bypass them. Surface is also the seam for legacy web and desktop apps.

---

## 2. Repo layout

```
core-replay/
├── README.md  REPORT.md  DESIGN.md  CLAUDE.md
├── pyproject.toml  Makefile  .gitignore  .env.example
├── config/
│   ├── policy.yaml               # allowed origins/actions, risk rules, redaction, limits
│   └── tenants/cu_alpha.yaml, cu_beta.yaml   # base_url, product version, overrides
├── mockbank/                     # TARGET APP: FastAPI, server-rendered, frameset, tables, no ids
│   ├── app.py  data.py  faults.py
│   └── templates/
├── cua/
│   ├── cli.py                    # cua discover | validate | approve | list | replay
│   ├── config.py                 # load policy + tenants
│   ├── catalog.py                # load / save / approve / list artifacts
│   ├── schema/                   # artifact.py, result.py, trace.py   (source of truth)
│   ├── surface/                  # base.py (protocol), playwright_web.py, locators.py
│   ├── session/provider.py       # login + reauthenticate, credentials from env only
│   ├── safety/                   # policy.py (PolicyGate), redact.py
│   ├── discovery/                # agent, observe, tools, prompts, stuck, recorder
│   ├── compiler/                 # compile.py + one module per pass
│   ├── replay/                   # executor, preflight, resolver, checks, extract, overrides
│   ├── handoff/                  # controller (state machine), models, recorder, operator (+ page)
│   └── evidence/logger.py        # run.jsonl, step screenshots, DOM on failure, PW trace
├── capabilities/                 # artifact.schema.json (generated) + <product>/<capability>/<semver>.json
├── evidence/                     # curated demo runs (see evidence/README.md)
├── examples/results.json
├── scripts/export_schema.py
└── tests/
```

**The Surface protocol** (async) is the seam between "how we perceive and act" and "the recorded flow":

```python
class Surface(Protocol):
    async def observe(self, with_screenshot: bool = False) -> Observation: ...         # a11y tree + refs (+ masked screenshot)
    async def resolve(self, target_id: str, target: Target) -> Resolved: ...           # ranked locators + MatchRule → exactly one element
    async def act(self, action: Action, resolved: Resolved | None, risk: RiskClass, *, value: str | None) -> None: ...  # policy-gated
    async def check(self, predicate: Predicate, targets: dict[str, Target]) -> bool: ...
    async def read(self, resolved: Resolved) -> str: ...
    async def snapshot(self, reason: str, *, dom: bool = False) -> list[str]: ...     # masked evidence paths
```

`PlaywrightWebSurface` implements all six. A `DesktopSurface` would implement the same six over Windows UIA or macOS AX: `role`, `label` and `text` map directly, and `table_cell` maps to grid patterns. The artifact does not change.

---

## 3. Artifact schema: why it's shaped this way

The schema lives in `cua/schema/artifact.py`, and `capabilities/artifact.schema.json` is generated from it (`make schema`). The hand-written example `1.0.0.json` is a fixture for building replay first. After the first real discovery run it moves to `tests/fixtures/`, and `capabilities/` holds only generated artifacts.

| Decision | Why |
|---|---|
| **Contract kept separate from implementation** (`contract` vs `targets` / `steps` / `conditions`) | A calling agent needs only inputs, outputs and outcomes, like a function signature. Implementation can change (new locators, a tenant override) without a contract change. |
| **Outcomes are declared** (`SUCCESS`, `MEMBER_NOT_FOUND`, `NO_SAVINGS_ACCOUNT`) | Business outcomes are part of the API, not errors. |
| **Targets defined once, steps refer to them by id** | Reviewers see each control once, with its rationale. Tenant overrides patch `targets.<id>` only. |
| **Ranked locators, semantic first** (role → label → text / table_cell / near_text → css / xpath → coordinates) | What a human sees is stable in slow-changing enterprise UIs and exists on desktop too. The validator requires at least one semantic locator. |
| **`MatchRule` on every target** | Finding *an* element isn't enough; it must be the *right* one (e.g. the balance cell must look like money). Silent wrong reads become loud failures. |
| **`table_cell`, `near_text`, `frame_path`** | Built for legacy markup: no ids, nested tables, framesets. |
| **Templates only in `fill` / `navigate`** | No PII, tenant URLs or credentials in artifacts. Portable across tenants and safe to commit. |
| **Sensitivity on inputs and outputs** (`member_id`, `savings_balance` = `pii`) | Customer identifiers and balances are regulated data: hashed in logs, masked in screenshots. Conservative by default. |
| **`conditions` map** (detect → classify → handler) | The error taxonomy is reviewable data. The validator forces classification and handler to agree. |
| **`risk` per step, plus `policy`** | The riskiest step decides whether the caller must confirm. It is validated, so it can't be under-declared. |
| **`app` binding** = vendor product + version range + fingerprint | The artifact belongs to a product, not a tenant. A wrong app or version is caught before step 1. |
| **`provenance` + `review`** (draft → approved) | Audit trail. Only approved artifacts replay unattended. |
| **Semver per artifact; `schema_version` for the format** | Major = contract change, minor = new outcome or condition, patch = locator fix. Approved versions are immutable. |

---

## 4. Result contract and error taxonomy

The contract lives in `cua/schema/result.py`, with examples in `examples/results.json`.

| Class | Examples | Replay behavior | `RunResult.status` |
|---|---|---|---|
| **Rejected before UI** | bad input, draft artifact run unattended, irreversible capability without `--confirm` | never touch the app | `rejected` |
| **Business outcome** | member not found, no savings account | stop, return the declared `outcome_code` | `business_outcome` |
| **Recoverable** | system notice popup (dismiss), "Processing…" (retry), session expired (reauthenticate once) | bounded recovery, log a `Recovery`, re-enter the race | continues. If exhausted → `RECOVERY_EXHAUSTED` (session: `SESSION_EXPIRED`) → escalate |
| **Hard failure** | permission denied, app error, target not found or ambiguous, checkpoint failed, **unknown state** | stop, snapshot, `fail` or `escalate` | `failed` (step, expected, observed, evidence) |

`UNKNOWN_STATE` is the most important category: the screen matches neither the checkpoint nor any known condition. Replay never guesses. It snapshots and escalates.

---

## 5. Replay algorithm (deterministic)

```
load artifact → apply tenant overrides → effective artifact (hash logged)
pre-flight on the effective artifact:                     → rejected
    review status vs mode · irreversible needs --confirm · inputs vs contract (type, pattern)
SessionProvider.login (credentials from env)
surface.check(app.fingerprint)                            → failed / APP_VERSION_MISMATCH
for step in steps:
    control.ensure_automation()                           # handoff seam
    gate.authorize(action, risk, mode, url, confirmed)    # policy, every action
    el = resolve(target)        # locators in order; first with exactly one match passing MatchRule
                                # none → TARGET_NOT_FOUND / TARGET_AMBIGUOUS; index>0 logged as drift
    surface.act(action, el, value=<resolved template>)
    ── RACE until step.expect.timeout, polling every limits.poll_interval_ms ──
       1. watched conditions, in declared order → first match → handler
             business    → return business_outcome
             recoverable → dismiss / retry_step / reauthenticate (bounded) → re-enter race
             hard        → fail | escalate
       2. step.expect satisfied (or none) → next step
       timeout, nothing matched → UNKNOWN_STATE → snapshot + escalate
    ────────────────────────────────────────────────────────────────────────
    extract: read → parse_value (currency/decimal/date) → type-check vs OutputSpec
assert artifact.success → RunResult(success, outputs)
```

Determinism comes from: fixed step order and condition precedence, waits on predicates (never `sleep`), bounded retries, and no model calls. The same inputs on the same screen always give the same decision.

---

## 6. Handoff: the control-transfer model

Implemented in `cua/handoff/controller.py` (`SessionControl`), one per live session. It tracks `state`, `holder` and an `epoch` that increments on every transfer.

```
AUTOMATION ──request_intervention()──► PAUSED ──take_control(op)──► HUMAN
HUMAN ──hand_back()──► RESUMING ──resumed()──► AUTOMATION         (resync found a satisfied checkpoint)
RESUMING ──request_intervention()──► PAUSED                        (resync failed: ask again, with expected state)
PAUSED | HUMAN ──abort() / timeout──► ABORTED → RunResult failed, handoff.resolution = aborted | timed_out
```

- **Triggers.** Replay: an `escalate` handler, `UNKNOWN_STATE`, or recovery exhausted. Discovery: the stuck detector (step budget, same screen 3×, 3 failed actions), the model calling `ask_human`, or policy `NeedsHuman` (an irreversible action). Irreversible steps in replay are refused in pre-flight, never escalated mid-transaction.
- **Same live session.** The operator API runs in-process on :8001 in the same event loop as the executor. The human uses the same headed Chromium window, so context, cookies and page are never recreated. Remote streaming (CDP screencast or noVNC) is mocked and documented.
- **Intervention request** (`handoff/models.py`): run, capability or goal, step and intent, reason, category, current URL, expected state, masked screenshot.
- **Recording.** `expose_binding` plus an injected listener capture clicks and changes as `HumanAction`s with semantic hints. Typed values are reduced to `«redacted:len=n»`.
- **Resync.** The executor doesn't trust "I fixed it". It evaluates the `expect` checks of the current and following steps on the live screen, and resumes after the furthest step whose checkpoint holds.
- **Discovery** resumes the LLM loop with the human's actions in its history. Those steps are marked in `provenance.human_assisted_steps`.

---

## 7. Safety model

| Guardrail | Where it's enforced |
|---|---|
| Origin allowlist | `context.route("**/*")` in Surface (network level: all frames and redirects) + `PolicyGate.authorize` |
| Action-type allowlist | `PolicyGate.authorize`, before every action, for both the LLM and replay |
| Risk gate | Discovery: irreversible → `NeedsHuman` (escalate). Replay: irreversible capability needs `--confirm` **and** an approved artifact, checked in pre-flight. A false stop costs a minute; a wrong commit costs a remediation. |
| Credentials | `SessionProvider` reads env only. Never sent to the LLM, logged, or stored in artifacts. |
| No literals in artifacts | Schema validator (templates only) |
| Redaction | `pii` → salted hash in logs; `secret` → `«secret:name»`; human-typed → length only; `sensitive` targets masked in screenshots; pre-flight errors never echo values |
| Untrusted page content | Wrapped as `<page untrusted="true">` in prompts; policy blocks the consequences regardless of what the model is told |
| Limits | Discovery uses synthetic data only. In production, observations sent to the model would also need masking. |

---

## 8. Multi-tenant and heterogeneous surfaces (design only)

- **Artifacts belong to the product** (`mockbank_core >=2.0,<3.0`). Tenants supply `{{tenant.*}}` values and an optional override patch. Patches may touch only `targets` and `conditions`, never `contract` (enforced in `replay/overrides.py`). Effective artifact = base + patch, hash logged per run. Example: `cu_beta` relabels "Member Number" to "Account Holder #".
- **Drift detection:** fingerprint mismatch; a fallback locator winning (locator-degradation metric per tenant); repeated checkpoint failures or unknown states for one tenant; humans repeatedly making the same fix during handoffs. Any of these flags (artifact, tenant) for re-discovery.
- **Legacy web:** `frame_path`, `table_cell`, `near_text`.
- **Desktop:** a new `Surface` over UIA or AX. Coordinates plus a `MatchRule` text check are the last resort (e.g. Citrix).

---

## 9. Build order (thin slice first)

Build order is not runtime order. Replay is built first against the hand-written fixture, because it is the production path and defines the artifact contract the compiler must produce.

1. **Mock bank** (frameset, tables, faults, synthetic members)
2. **Surface + SessionProvider + policy + evidence**
3. **Replay executor** (enable `tests/test_replay_mockbank.py`)
4. **Discovery + compiler**, then a real LLM run committed to `evidence/`
5. **Handoff** (operator in-process, recorder, resync)
6. **README / REPORT / evidence**
7. Stretch, pick one: prompt-injection demo, replay stability (`--repeat N`), or capability catalog as tools

**Demo commands** (also in the `Makefile`):

```
make mockbank                                                              # :8000
uv run cua discover --tenant cu_alpha --goal "look up member 10001 and read the savings balance"
uv run cua validate capabilities/mockbank/member.lookup_savings_balance/1.0.0.json
uv run cua approve mockbank.member.lookup_savings_balance --reviewer <you>
uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=10002   # success
uv run cua replay ... --input member_id=99999                                # business_outcome MEMBER_NOT_FOUND
uv run cua replay ... --input member_id=10001 --fault session_expired        # recovers via reauthenticate
uv run cua replay ... --input member_id=10001 --fault maint                  # UNKNOWN_STATE → operator on :8001
```

---

## 10. Decisions log

Add a row each time you make a non-obvious decision. This feeds `REPORT.md` and the interview.

| Date | Decision | Alternatives considered | Why |
|---|---|---|---|
| | Python + Playwright async + FastAPI, single process | Flask (sync) + separate operator process | Handoff needs executor and operator in one event loop on the same live browser |
| | Accessibility tree as primary observation, screenshot secondary | Screenshot + coordinates only | Works without a clean DOM; yields semantic locators replay can reuse |
| | Replay built before discovery, against a fixture | Discovery first | Replay is the production path and defines the artifact contract; one unknown at a time |
| | Irreversible steps gated in pre-flight, not mid-run | Escalate when reached | Never stop halfway through a transaction |
| | Session expiry recoverable via SessionProvider (reauthenticate once) | Always escalate | Credentials never touch the LLM or artifact; one bounded retry is safe |
| | Member id and balances classified `pii` | `internal` | Regulated financial data: conservative by default |
| | Tenant overrides limited to `targets` / `conditions` | Free-form patches | The contract (API) must be identical for every tenant |