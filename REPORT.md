# CoreReplay: Design Report

## 1. Architecture

```text
goal ─► DISCOVERY (LLM) ─► trace ─► COMPILER (no LLM) ─► artifact ─► SELF-TEST replay ─► catalog (draft → approved)
caller inputs ─► REPLAY (no LLM) ─► RunResult: success | business_outcome | failed | rejected
                    └─ unknown state ─► HANDOFF: pause → human on the SAME session → hand back → resync → resume
```

**Everything that touches the app goes through one `Surface`.** That includes the LLM's actions, replay
and resync. The surface applies the policy gate, the input lock and redaction, and writes the evidence.
Neither path can bypass it, and it is also the seam where legacy-web and desktop surfaces plug in
(section 4).

The key decisions and their trade-offs:

| Decision | Why | Cost |
| --- | --- | --- |
| One Python process, async Playwright; the operator API runs as a task in the same event loop | Taking control acts directly on the live `SessionControl` and browser: no IPC, no copied state | One machine per run; a real deployment would stream the browser to the operator |
| The LLM observes the **accessibility tree**, not pixels | Works without a clean DOM; what it acts on is also what replay can find again (role, label, text) | Pure canvas or Citrix screens need the coordinate fallback (designed, not built) |
| Replay was built **before** discovery, against a hand-written artifact | Replay is the production path, so the artifact contract came first and discovery had to compile into it | Two artifacts to keep aligned while building (the fixture is now a test input) |
| The compiler is deterministic and gated by a **self-test** | The draft is replayed in a fresh browser context with the discovery inputs, and saved only if outputs match and no fallback locator was needed | One extra replay per discovery |
| Files on disk, no database | The catalog is versioned JSON in git; evidence is JSONL plus PNGs | No concurrent writers or query layer (out of scope) |

**Stack.** Python 3.11, Playwright (Chromium), Pydantic v2 for every schema, FastAPI for the mock bank and
the operator page, Typer for the CLI. Discovery uses `claude-opus-5-5` with strict tool schemas, one tool
call per turn, and an append-only, prompt-cached history. The target app, **MockBank**, is legacy on
purpose: a frameset, table layouts, no ids and no `<label for>`, with synthetic members and switchable
faults.

## 2. Artifact schema

An artifact is **a function signature plus its implementation**, for a UI with no API
(`cua/schema/artifact.py`; `capabilities/artifact.schema.json` is generated from it). Each part has a
different reader:

- **Contract (what the calling agent reads).** Typed `inputs` (with sensitivity and a regex checked
  before the UI is touched), typed `outputs`, and declared `outcomes`. `MEMBER_NOT_FOUND` is a return
  value, not an error. The `policy` block declares `max_risk` and `requires_confirmation`.
- **Implementation (what replay reads).**
  - `targets`: each control is defined once, with ranked locators, a `MatchRule`, a `sensitive` flag
    and a written rationale. Steps refer to targets by id.
  - `steps`: an action, a `risk` level and an `expect` checkpoint each.
  - `conditions`: the error taxonomy as data, *detect → classify → handle*.
  - `success`: a final check before the run reports SUCCESS.
- **Governance (what reviewers and auditors read).**
  - `app` binds the artifact to a vendor product and version range, plus a fingerprint, not to a tenant.
  - `provenance` records the discovery run, model and human-assisted steps.
  - `review` holds the status (draft → approved).
  - The artifact itself is versioned with semver: major for a contract change, minor for a new
    outcome or condition, patch for a locator fix.

```json
"share_savings_balance_cell": {
  "locators": [{ "strategy": "table_cell", "table_has_header": "Account Type",
                 "row_contains": "Share Savings", "column_header": "Balance" }],
  "frame_path": ["main"], "sensitive": true,
  "match": { "unique": true, "visible": true, "text_pattern": "^-?[$€£]?-?[0-9,]+\\.[0-9]{2}$" } }
```

The validator enforces what a reviewer would otherwise have to check by hand:

- Every target has at least one semantic locator (role, label, text, `table_cell` or `near_text`).
  CSS, XPath and coordinates are fallbacks only.
- `fill` and `navigate` values must be templates (`{{inputs.x}}`, `{{tenant.x}}`), so no PII or tenant
  URL can live in an artifact.
- Every reference resolves: targets, steps, outputs and outcome codes.
- A condition's classification must agree with its handler: a business outcome can't "fail", and a hard
  failure can't be "dismissed".
- `max_risk` must equal the riskiest step, and any irreversible step forces `requires_confirmation`.

The compiler drops positional fallbacks (`nth-of-type`). On a recorded discovery trace, the positional fallback for
the balance cell pointed at row 2, which for another member is the Checking balance, and its currency
pattern would still have accepted the value. A loud TARGET_NOT_FOUND beats a silent wrong read.

## 3. Determinism & error handling

**Pre-flight (no browser) → `rejected`.** Replay refuses up front:

- unknown or invalid inputs (and never echoes the values),
- a draft artifact in unattended mode,
- a deprecated artifact,
- an irreversible capability without `--confirm`,
- a tenant whose product version is outside `app.version_range`,
- templates it cannot resolve.

**Per step.** Replay resolves the target: it tries the ranked locators in order, accepts the first that
matches exactly one element passing its `MatchRule`, and polls within a bounded wait. It then acts, and
**races** the result. Every 250 ms it checks the watched conditions in their declared order, then the
step's checkpoint, until the step's deadline. There are no `sleep()` calls and no LLM; the same inputs on
the same screen give the same decision.

| Class | Examples (MockBank fault) | Replay behaviour | Result |
| --- | --- | --- | --- |
| Business outcome | no such member; no savings row (derived by the compiler) | stop, return the declared code | `business_outcome` |
| Recoverable | notice dialog (dismiss, at most 2); "Processing…" (wait until clear, at most 3, never re-submits); session expired (re-authenticate once) | bounded recovery, logged, then re-race | continues |
| Hard failure | permission denied, app error | stop, masked screenshot plus redacted DOM | `failed` + category, step, expected, observed |
| Unknown state | screen matches neither the checkpoint nor any condition ("Maintenance Window") | **never guess**: snapshot, escalate | handoff, or `failed UNKNOWN_STATE` |

**Routing rule.** Escalate when a human at the screen could fix the problem: an unknown state, a missing
target, recovery exhausted, or re-authentication failed. Fail without escalating when a click can't or
mustn't fix it: a policy violation, a wrong app version, or a checkpoint failure (a wrong value was read).

**Guards against false positives:**

- A condition based on `target_absent` must still hold one poll later, so a header painted before its
  table doesn't read as "no savings account".
- Recovery budgets are per run, per condition.
- Extracted text is parsed into the contract type (money stays an exact decimal string). Text that
  doesn't parse fails the run instead of returning garbage.

**Drift (secondary).** The winning locator index is logged for every target, and index > 0 is a drift
signal per tenant and target. The self-test refuses any draft whose targets did not resolve by their
first locator.

## 4. Heterogeneity & multi-tenant

**Surface seam.** The recorded flow never mentions Playwright. `Surface` exposes a small set of
operations (observe, resolve, act, check, read, snapshot), and the artifact speaks in roles, labels, table
cells and captions. Legacy web is implemented: `frame_path`, plus custom `table_cell` and `near_text`
selector engines, so the locators survive framesets and tables without ids. A desktop surface would
implement the same methods over Windows UIA or macOS AX. Role, label and text map directly, `table_cell`
maps to grid patterns, and coordinates plus a `MatchRule` text check are the last resort for Citrix.
**Not yet done:** the executor constructs the Playwright surface directly; dispatching on `app.surface` is
the next step.

**Multi-tenant reuse.**

- **Artifacts belong to the vendor product** (`mockbank_core >=2.0,<3.0`), not to a tenant.
- **A tenant config** supplies `base_url`, the names of its credential env vars, PII masks, and an
  optional override patch.
- **Overrides may patch only `targets` and `conditions`, never the contract**, so the API is identical
  for every tenant. The effective artifact is re-validated and its digest logged per run.
- **Errors common to a product live in a reviewed condition pack**
  (`config/products/mockbank_core.conditions.yaml`). Every capability compiled for that product
  inherits it.
- **Drift is caught by:** a fingerprint mismatch, a version outside the range (rejected in pre-flight),
  fallback locators winning, repeated unknown states for one tenant, and humans repeating the same fix.
  Any of these flags the (artifact, tenant) pair for re-discovery.

## 5. Escalation & handoff

**Detecting "stuck".**

- In replay: an unknown state, a missing target, recovery exhausted, a re-authentication failure, or a
  condition whose handler is `escalate`.
- In discovery: a step budget, the same screen three times, three failed actions, the model calling
  `ask_human`, or the policy demanding a human for an irreversible action.

**The intervention request** carries the capability or goal, the step and its intent, the reason and
category, the URL (no query string), the expected state and a masked screenshot.

**Control transfer.** `SessionControl` is a state machine, one per live session:

```text
AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION
RESUMING → PAUSED (resync failed: ask again)      PAUSED | HUMAN → ABORTED (abort, timeout)
```

- Automation checks `ensure_automation()` before every action.
- The operator page (in-process, loopback only, with a random token per run) drives the transitions.
- The human uses the **same headed browser**: same context, cookies and page.
- An **input lock** makes the control rule hold in the browser too. While automation holds control,
  every document swallows pointer and key input. It opens only in HUMAN, or for automation's own single
  action. It was added after a real demo, in which a click during the race fixed the page and the run
  "succeeded" with an unrecorded human.
- A passive listener records the human's clicks, fills (length only), selects and navigations, redacted.

**Handing back.** Replay never trusts "I fixed it". It **resyncs**: it resumes after the furthest step from
the escalated one whose checkpoint holds on the live screen. If none holds, it retries the step when its
action never ran, and otherwise asks again, stating exactly what screen it needs.

- Hand-backs are bounded (3 per run).
- The run never continues past an irreversible step that a human performed.
- With no operator attached (unattended), an escalation fails at once.

[`evidence/human_handoff/`](evidence/human_handoff/) holds a real run: escalation, takeover, resync, success.

## 6. Safety

- **Allowlists.** Every request from every frame passes a network-level origin allowlist
  (`context.route`), and every action passes the `PolicyGate` (allowed action types plus a risk check),
  for the LLM and replay alike.
- **Risk.** Each step is classed `read`, `reversible` or `irreversible`.
  - Discovery: an irreversible action needs a human (handoff). The LLM may never commit on its own.
  - Replay: irreversible capabilities need the caller's `--confirm`, decided in pre-flight. Replay never
    escalates halfway through a transaction, because a false stop costs a minute and a wrong commit
    costs a remediation.
  - Unattended replay needs an approved artifact.
- **Credentials** come from env through the `SessionProvider` only. They are never shown to the LLM,
  never logged, and never captured in traces.
- **The LLM sees data only as its shape.** Data values become `«shape:currency»`, masked cells `«masked»`,
  and typed values `«redacted:len=n»`.
  - It may type only the caller's `--param` values, and every typed value becomes a template.
  - Pressing Enter is refused, so the risk of a submit can always be classified from its button.
  - Page content is wrapped as untrusted (`<page untrusted="true">`), with escaping so the page can't
    close the wrapper.
- **Redaction on write.** PII becomes a salted hash, secrets become `«secret:name»`, and human-typed
  values keep only their length.
- **Screenshots** mask sensitive targets, the tenant's PII selectors, text inputs and any other
  digit-bearing leaf. Evidence screenshots may show bank-style partial values (SSN last 4). Playwright
  traces are opt-in and never committed. An automated check verifies that the committed evidence holds
  none of the synthetic PII values.

**Limits:**

- Risk is classified from button names (`submit`, `confirm`, `transfer`…). An unlisted label defaults to
  `read`, so a human reviewer approving the artifact is the real safety net.
- The allowlist covers origins, not routes.
- Hashing is pseudonymisation, not anonymisation: low-entropy values like member numbers are only as
  safe as the salt.
- PII outside the listed selectors is masked only if it contains a digit.

## 7. Cuts

**Mocked deliberately:**

- The operator console: a minimal page; the human uses the local headed browser, with no remote
  streaming.
- Operator authentication: the per-run token prevents cross-site requests (CSRF) but identifies no one.
- Desktop surfaces: design only.

**Known gaps I would fix next:**

- **Resync checks the screen, not the data.** The handoff evidence shows a human re-running the search,
  and checkpoints cannot tell which member they typed. Next: bind the session to the request's inputs.
- **Discovery's 300 s budget includes the time a human holds control.** Expiring mid-handoff ends the run
  with an internal error. Next: pause the clock while the state is HUMAN.
- **Tenant overrides are keyed by compiler-generated target ids,** so the `cu_beta` example matches the
  hand-written fixture but not the compiled artifact. Next: version overrides per artifact major, or
  key them by semantic description.
- **Approval is not bound to content,** and an approved file edited later stays "approved". Next: store a
  content digest in `review` and check it in pre-flight.
- **MockBank's flow is read-only.** The irreversible-action gate is unit-tested but never exercised
  against a live commit. Next: an "open sub-account → confirm" screen.
- **The risk classifier fails open on unknown labels, and the allowlist has no routes.** Next: default
  unknown clicks to `reversible`, and add a path allowlist.
- **No overall run deadline.** Each step and recovery is bounded, but the run as a whole is not.

**Next, beyond fixes:** expose the catalog as callable tools for an agent; replay stability (N runs, a
flakiness score); a bounded, policy-checked LLM fallback for a single step; and a surface factory by
`app.surface`.
