# Design sketch: computer-use capability system

Working notes for the build. Much of this becomes `REPORT.md` later.

---

## 1. The whole system on one page

```
                 goal + target
                      │
      ┌───────────────▼────────────────┐
      │  DISCOVERY  (LLM in the loop)  │  observe → decide → act, max N steps
      │  agent.py + prompts.py         │
      └───────────────┬────────────────┘
                      │ raw trace (actions + element snapshots, redacted)
      ┌───────────────▼────────────────┐
      │  COMPILER  (no LLM required)   │  parameterize values, pick ranked locators,
      │  compiler.py                   │  add checkpoints, attach known conditions
      └───────────────┬────────────────┘
                      │ CapabilityArtifact (JSON, status=draft)  ──► human review ──► approved
      ┌───────────────▼────────────────┐
      │  REPLAY  (no LLM)              │  inputs → steps → checkpoint/condition race
      │  executor.py                   │  → RunResult {success | business_outcome | failed | rejected}
      └───────────────┬────────────────┘
                      │ stuck / risky / unknown state
      ┌───────────────▼────────────────┐
      │  HANDOFF                       │  pause → intervention request → human drives SAME
      │  controller.py + operator UI   │  browser → recorded → hand back → re-sync → resume
      └────────────────────────────────┘

 Everything touches the app ONLY through  Surface  (the seam), which runs every call
 through  Policy  (allowlist + risk gate) and writes to  Evidence  (redacted JSONL + screenshots).
```

**The main decision:** both the LLM and replay act through the same `Surface` interface. Guardrails, redaction and evidence are enforced there, at one chokepoint, so neither path can bypass them. Surface is also the seam for legacy web and desktop apps.

---

## 2. Repo layout

```
core-replay/
├── README.md                     # setup + exact demo commands
├── REPORT.md                     # the 7 required headings
├── pyproject.toml
├── .env.example                  # ANTHROPIC_API_KEY=...  (never commit .env)
├── config/
│   ├── policy.yaml               # allowed origins, allowed action types, risk rules, redaction
│   └── tenants/
│       ├── cu_alpha.yaml         # base_url, product version, overrides file (if any)
│       └── cu_beta.yaml
├── mockbank/                     # the TARGET APP (stand-in for a legacy core)
│   ├── app.py                    # Flask/FastAPI, server-rendered, frameset (nav|main), tables, no test ids
│   ├── templates/
│   ├── data.py                   # synthetic members only
│   └── faults.py                 # ?fault=not_found|notice|slow|session_expired|denied|error|maint
├── cua/
│   ├── schema/                   # ✅ done: artifact.py, result.py
│   ├── surface/
│   │   ├── base.py               # Surface protocol (below)
│   │   └── playwright_web.py     # implementation: a11y tree + DOM + screenshot, frame-aware
│   ├── discovery/
│   │   ├── agent.py              # LLM loop, tool-calling, step budget, stuck detection
│   │   ├── prompts.py
│   │   └── compiler.py           # trace → artifact
│   ├── replay/
│   │   ├── executor.py           # the deterministic engine
│   │   ├── resolver.py           # Target → element (ranked locators + MatchRule)
│   │   └── checks.py             # Predicate/Check evaluation, condition race
│   ├── safety/
│   │   ├── policy.py             # allowlist + risk gate, called inside Surface.act()
│   │   └── redact.py             # value hashing/masking, screenshot masks
│   ├── handoff/
│   │   ├── controller.py         # control lease + state machine
│   │   ├── recorder.py           # captures human actions via injected listener
│   │   └── operator.py           # minimal web page: queue, "Take control", "Hand back"
│   ├── evidence/logger.py        # run.jsonl, step screenshots, DOM snapshot on failure, PW trace
│   └── cli.py                    # cua discover | cua replay | cua operator | cua validate
├── capabilities/                 # the catalog (artifacts are data, versioned, PR-reviewed)
│   ├── artifact.schema.json      # ✅ generated from pydantic, for non-Python consumers
│   └── mockbank/member.lookup_savings_balance/1.0.0.json   # ✅
├── evidence/                     # committed demo runs
│   ├── discovery_<id>/
│   ├── replay_<id>_success/
│   ├── replay_<id>_not_found/
│   └── replay_<id>_handoff/
└── tests/                        # ✅ test_schema.py; + executor tests against mockbank faults
```

**The Surface protocol** is the seam between "how we perceive and act" and "the recorded flow":

```python
class Surface(Protocol):
    def observe(self) -> Observation: ...            # a11y tree (+ screenshot) — what the LLM sees
    def resolve(self, target: Target) -> Resolved: ...  # ranked locators + MatchRule → one element or error
    def act(self, action: Action, resolved: Resolved | None, risk: RiskClass) -> None: ...  # policy-gated
    def check(self, predicate: Predicate) -> bool: ...
    def read(self, resolved: Resolved) -> str: ...
    def snapshot(self, reason: str) -> EvidenceRef: ...  # screenshot (+DOM) with sensitive targets masked
```

`PlaywrightWebSurface` implements all six. A `DesktopSurface` would implement the same six over Windows UIA or macOS AX: `role`, `label` and `text` locators map directly, and `table_cell` maps to grid patterns. The artifact does not change.

---

## 3. Artifact schema: why it's shaped this way

The file is `cua/schema/artifact.py`, and the example is `capabilities/mockbank/.../1.0.0.json`.

| Decision | Why |
|---|---|
| **Contract kept separate from implementation** (`contract` vs `targets` / `steps` / `conditions`) | A calling agent needs only inputs, outputs and outcomes, like a function signature. Implementation can change (new locators, a tenant override) without a contract change, and without a major version bump. |
| **Outcomes are declared** (`SUCCESS`, `MEMBER_NOT_FOUND`, `NO_SAVINGS_ACCOUNT`) | Business outcomes are part of the API, not errors. The agent can branch on them. |
| **Targets defined once, steps refer to them by id** | Reviewers see each control once, with its rationale. Tenant overrides patch `targets.<id>` only. |
| **Ranked locators, semantic first** (role → label → text / table_cell / near_text → css / xpath → coordinates) | The a11y role and visible text are what a human sees. They are stable in slow-changing enterprise UIs and also exist on desktop. CSS and XPath are allowed only as fallbacks (the validator enforces this). |
| **`MatchRule` on every target** (unique, visible, enabled, `text_pattern`) | Finding *an* element isn't enough; it must be the *right* one. For example, the balance cell must look like money. This turns silent wrong reads into loud failures. |
| **`table_cell` and `near_text` strategies** | Built for legacy markup: row-by-label and column-by-header survive reordering, with no ids needed. |
| **`frame_path`** | Legacy framesets and iframes are the norm, so frame scoping is part of the target. |
| **Templates only in `fill` / `navigate`** (`{{inputs.x}}`, `{{tenant.base_url}}`, `{{secrets.x}}`) | The artifact never contains PII, tenant URLs or credentials. It is portable across tenants and safe to commit. |
| **`conditions` map** (detect → classify → handler) | The error taxonomy is data, per capability, and reviewable. The validator forces classification and handler to agree. |
| **`risk` per step, plus `policy`** | The riskiest step decides whether the caller must confirm. It is validated, so it can't be under-declared. |
| **`app` binding** = vendor product + version range + fingerprint | The artifact belongs to a product, not a tenant. The fingerprint check before step 1 catches "wrong app or version" early (`APP_VERSION_MISMATCH`). |
| **`provenance` + `review`** (draft → approved) | Audit trail. Only approved artifacts replay unattended; drafts run supervised. |
| **Semver** | Major = contract change. Minor = new outcome or condition. Patch = locator fix. |

---

## 4. Result contract and error taxonomy

The file is `cua/schema/result.py`, and the examples are in `examples/results.json`.

| Class | Examples | Replay behavior | `RunResult.status` |
|---|---|---|---|
| **Business outcome** | member not found, no savings account | stop and return a declared `outcome_code` | `business_outcome` |
| **Recoverable** | system notice popup, "Processing…" page | dismiss or retry (bounded), log a `Recovery` | continues; ends `success` or escalates as `RECOVERY_EXHAUSTED` |
| **Hard failure** | session expired, permission denied, app error, target not found, ambiguous target, **unknown state** | stop, snapshot, `fail` or `escalate` | `failed` (with step, expected, observed, evidence) |
| **Rejected before UI** | bad input, policy violation, draft artifact run unattended | never touch the app | `rejected` |

`UNKNOWN_STATE` is the most important category: the screen matches neither the checkpoint nor any known condition. Replay never guesses. It snapshots and escalates.

---

## 5. Replay algorithm (deterministic)

```
validate inputs against contract (pattern, type)          → rejected / INPUT_INVALID
check artifact.review.status vs run mode                  → rejected
resolve tenant overrides → effective artifact (logged hash)
surface.check(app.fingerprint)                            → failed / APP_VERSION_MISMATCH
for step in steps:
    controller.ensure_automation_holds_lease()            # handoff seam
    policy.authorize(step, confirmation)                  # before any action
    el = resolver.resolve(target)   # try locators in order; first that passes MatchRule
                                    # 0 → TARGET_NOT_FOUND, >1 → TARGET_AMBIGUOUS
    surface.act(step.action, el)
    ── RACE until step.expect.timeout ──────────────────────────────
    each poll tick (e.g. 250ms), in this fixed order:
       1. watched conditions (declared order) → first match → run handler
             business    → return business_outcome
             recoverable → dismiss/retry (bounded), re-enter race
             hard        → fail or escalate
       2. step.expect satisfied → next step
    timeout: nothing matched → UNKNOWN_STATE → escalate
    ───────────────────────────────────────────────────────────────
    extract steps: read → parse (currency/decimal/date) → type-check vs OutputSpec
assert artifact.success → return success + outputs
```

Where the determinism comes from:

- A fixed step order and a fixed condition precedence.
- Explicit waits on predicates, never `sleep`.
- Bounded retries.
- No model calls.
- The same inputs and screen always give the same decision.

---

## 6. Handoff: the control-transfer model

**A control lease, one per live session.** It records `holder ∈ {automation, human:<op_id>, none}` and an `epoch` counter. Every automation action first checks `ensure_automation_holds_lease()`, so the automation cannot act while a human holds control.

```
RUNNING ──(stuck / escalate / risky step needs approval)──► PAUSED_AWAITING_HUMAN
PAUSED_AWAITING_HUMAN ──(operator clicks "Take control")──► HUMAN_CONTROL   (lease → human, epoch++)
HUMAN_CONTROL ──(operator clicks "Hand back")──► RESYNCING                   (lease → automation)
RESYNCING ──(screen matches expect of step k, or success check)──► RUNNING from step k+1
RESYNCING ──(no match)──► PAUSED_AWAITING_HUMAN   (tell operator what state is expected)
any ──(operator "Abort" / timeout)──► ABORTED → RunResult failed, handoff.resolution=aborted
```

- **Same live session.** Playwright launches Chromium headed, with a CDP port. The operator works in that same window: locally the operator page links to it; remotely it would be streamed over noVNC or CDP screencast (that part is mocked). The browser context, cookies and page are never recreated.
- **The intervention request** carries the capability id, step id and intent, the reason or category, a masked screenshot, the expected state and the run id. It goes to an in-memory queue, which the operator page polls. A real system would use a pub/sub queue plus Slack or pager notifications.
- **Recording what the human did.** `page.expose_binding` plus an injected listener capture click, change and submit events as `HumanAction`s with semantic target hints. Fill values are recorded as `«redacted:len=n»`. The same listener gives discovery a path to learn from human steps later (`provenance.human_assisted_steps`).
- **Resuming.** The executor does not trust "I fixed it". It re-syncs by evaluating the `expect` checks of the current and following steps against the live screen, and resumes after the furthest step whose checkpoint holds.
- **In discovery.** "Stuck" means the step budget is exhausted, the same observation repeats 3 times, the model asks for help, or policy blocks a risky action.

---

## 7. Safety model

| Guardrail | Where it's enforced |
|---|---|
| Origin allowlist (`policy.yaml`) | `Surface.act` / `navigate`: every request URL is checked, including frame navigations, and the route is intercepted. |
| Action-type allowlist | `Surface.act`, for both the LLM and replay. |
| Risk gate | `irreversible` steps require the caller to pass `confirm=true` **and** an approved artifact. In discovery, irreversible actions always escalate to a human. Justification: in banking, a false stop costs a minute, while a wrong commit costs a remediation. |
| No literals in artifacts | Schema validator (templates only). |
| Redaction | `pii` inputs and outputs are hashed in logs (`«member_id:sha256:ab12…»`). `sensitive` targets are masked in screenshots (Playwright `mask=`). Secrets are never logged. |
| LLM exposure | Discovery runs against synthetic data only. Document the limit: in production, the observation sent to the model would itself need masking. |

---

## 8. Multi-tenant and heterogeneous surfaces (design only)

- **Artifact belongs to the product:** `mockbank_core >=2.0,<3.0`. The tenant supplies `{{tenant.*}}` values and an optional **override patch** keyed by target id or condition id. For example, tenant B renamed "Member Number" to "Account Holder #", so it overrides `targets.member_id_field.locators`. Effective artifact = base + patch, and its hash is logged per run.
- **Drift detection:**
  - The fingerprint check fails.
  - The winning locator is no longer the first one (a fallback was used) — log it as a *locator-degradation* metric per tenant.
  - Checkpoints fail repeatedly for a single tenant.

  Any of these flags the (artifact, tenant) pair for re-discovery, before users notice.
- **Legacy web** already works through `frame_path`, `table_cell` and `near_text`.
- **Desktop apps** need a new `Surface` implementation. Role, label, text and table locators map to the OS accessibility APIs. Coordinates plus a `MatchRule` text check are the last resort, e.g. for Citrix.

---

## 9. Build order (thin slice first)

1. **Mock bank app** with frameset, tables and fault switches. Seed synthetic members.
2. **Surface + policy + evidence logger.** This is the chokepoint everything uses.
3. **Replay executor**, tested with the hand-written example artifact (it already exists). Tests: success, not found, notice dismissed, slow, session expired, unknown state.
4. **Discovery agent** (Anthropic API, a11y tree + screenshot, tool-calling), then the **compiler** from trace to artifact. Commit a real run to `evidence/`.
5. **Handoff:** lease, pause, operator page, recorder, resync.
6. `README` demo commands and `REPORT.md`.
7. Stretch, pick one: a capability catalog exposed as tool definitions, or a base-vs-variant tenant override demo.

**Demo commands to aim for in the README:**
```
make mockbank                                     # starts target app on :5055
cua discover --target cu_alpha --goal "look up member 10001 and read their savings balance"
cua validate capabilities/mockbank/member.lookup_savings_balance/1.0.0.json
cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=10001
cua replay ... --input member_id=99999                          # → business_outcome MEMBER_NOT_FOUND
cua replay ... --input member_id=10001 --fault session_expired  # → escalation → operator → resume
cua operator                                      # operator page on :5056
```

---

## 10. Decisions log

Add a row each time you make a non-obvious decision. This feeds `REPORT.md` and the interview.

| Date | Decision | Alternatives considered | Why |
|---|---|---|---|
| | Python + Playwright async + FastAPI, single process | Flask (sync) + separate operator process | Handoff needs executor and operator in one event loop on the same live browser |
| | Accessibility tree as primary observation, screenshot as secondary | Screenshot + coordinates only | Works without a clean DOM, yields semantic locators that replay can reuse |
| | Irreversible steps gated in pre-flight, not mid-run | Escalate when reached | Never stop halfway through a transaction |
| | Session expiry = recoverable via session provider (re-auth once) | Always escalate | Credentials never touch the LLM or artifact; one bounded retry is safe |
