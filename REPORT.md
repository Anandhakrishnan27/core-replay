# CoreReplay: Design Report

## 1. Architecture

```text
goal ─► DISCOVERY (LLM) ─► trace ─► COMPILER (no LLM) ─► artifact ─► SELF-TEST replay ─► catalog (draft → approved)
caller inputs ─► REPLAY (no LLM) ─► RunResult: success | business_outcome | failed | rejected
                    └─ unknown state ─► HANDOFF: pause → human on the SAME session → hand back → resync → resume
```

The LLM is used once, at discovery. Production calls are deterministic replays of a compiled artifact,
so they cost no tokens and behave the same way every time. The cost: replay cannot improvise, so a
screen it has never seen goes to a human, not to a model.

Everything that touches the app goes through one `Surface`: the LLM's actions, replay and resync. It
applies the policy gate, input lock and redaction, and writes the evidence, so no path bypasses the
guardrails. It is also the seam for other surfaces (Section 4).

| Decision | Why | Cost |
| --- | --- | --- |
| The LLM observes the **accessibility tree**, not pixels | What it acts on (role, label, text) is what replay can find again | Canvas or Citrix screens need a coordinate fallback (designed, not built) |
| Replay was built **before** discovery | Replay is the production path, so its contract came first and discovery compiles into it | A hand-written fixture artifact to keep aligned |
| The compiler is deterministic and gated by a **self-test** | The draft is replayed in a fresh context and saved only if outputs match and no fallback locator was needed | One extra replay per discovery |
| One process; the operator API shares the event loop | Taking control acts on the live session and browser: no IPC, no copied state | One machine per run |

The stack is Python 3.11+, Playwright, Pydantic v2, FastAPI and Typer. Discovery defaults to
`claude-opus-5-5`. The target, MockBank, is legacy on purpose: frames, table layouts, no ids, synthetic
members and switchable faults.

## 2. Artifact schema

An artifact is a function signature plus its implementation, for a UI with no API
(`cua/schema/artifact.py`). It has three parts because it has three readers:

- **Contract (the calling agent):** typed `inputs` with sensitivity and a regex checked before the UI is
  touched, typed `outputs`, declared `outcomes` (`MEMBER_NOT_FOUND` is a return value, not an error), and
  a `policy` with `max_risk` and `requires_confirmation`.
- **Implementation (replay):** `targets` with ranked locators, a `MatchRule` and a `sensitive` flag;
  `steps`, each with a risk level and an `expect` checkpoint; `conditions` (detect, classify, handle);
  and a final `success` check.
- **Governance (reviewers):** `app` binds the artifact to a vendor product, version range and
  fingerprint, not a tenant; `provenance` records the discovery run and model; `review` holds the status.
  Versions are semver.

The committed artifact, `capabilities/mockbank/member.lookup_savings_balance/1.1.0.json`, was compiled
from the run in `evidence/discovery/` (provenance model `claude-opus-5-5`). One target:

```json
"share_savings_balance_cell": {
  "locators": [{ "strategy": "table_cell", "table_has_header": "Account Type",
                 "row_contains": "Share Savings", "column_header": "Balance" }],
  "frame_path": ["main"], "sensitive": true,
  "match": { "unique": true, "visible": true, "text_pattern": "^-?[$€£]?-?[0-9,]+\\.[0-9]{2}$" } }
```

The validator checks what a reviewer otherwise would. Every target needs a semantic locator (CSS, XPath
and coordinates are fallbacks only). Typed values must be templates, so no PII lives in an artifact.
References must resolve, and `max_risk` must equal the riskiest step.

The Pydantic model is the source of truth. `make schema` exports it to
`capabilities/artifact.schema.json`, so editors, reviewers and non-Python callers can check an
artifact's shape without running this code. The cost is that JSON Schema cannot express the
cross-field rules above, so a file can pass the schema yet fail `cua validate`. Replay always loads
artifacts through the Pydantic model.

The compiler drops positional fallbacks. In the recorded run, the balance cell's `nth-of-type` fallback
pointed at row 2, which for member 10002 is the Checking balance, and the currency pattern would have
accepted it. The trade-off: fewer locators means more loud TARGET_NOT_FOUND failures, which beats a
silent wrong read.

## 3. Determinism & error handling

Pre-flight runs before a browser opens. It returns `rejected` for invalid inputs (values never echoed),
a draft artifact in unattended mode, an irreversible capability without `--confirm`, a product version
outside `app.version_range`, or a tenant override that makes the artifact invalid.

Per step, replay takes the first ranked locator that matches exactly one element passing its
`MatchRule`, acts, then races: every 250 ms it checks the watched conditions in declared order, then the
step's checkpoint, until the step's deadline. There is no `sleep()` and no LLM, so the same inputs on the
same screen give the same decision. The cost is a declared timeout on every step and up to one poll of
latency.

| Class | Examples (MockBank fault) | Replay behaviour | Result |
| --- | --- | --- | --- |
| Business outcome | no such member; no savings row | return the declared code | `business_outcome` |
| Recoverable | notice (dismiss, max 2); "Processing" (wait, max 3, never re-submits); session expired (re-authenticate once) | bounded recovery, then race again | continues |
| Hard failure | permission denied; app error | masked screenshot, redacted DOM | `failed` with category, expected, observed |
| Unknown state | matches no checkpoint or condition ("Maintenance Window") | never guess: escalate | handoff, or `failed UNKNOWN_STATE` |

The routing rule: escalate when a human at the screen could fix it (unknown state, missing target,
exhausted recovery, failed re-authentication). Fail without escalating when a click cannot or must not
fix it (policy violation, wrong app version, failed checkpoint after a read). To avoid false positives, a
`target_absent` detector must still hold one poll later, and unparseable extracted text fails the run.

For UI drift, the winning locator index is logged per target, and an index above 0 is a drift signal.
The self-test refuses drafts that needed a fallback. Drift is detected, not repaired.

## 4. Heterogeneity & multi-tenant

**The surface seam (built for legacy web; desktop is design only).** The recorded flow never mentions
Playwright. `Surface` exposes observe, resolve, act, check, read and snapshot, and the artifact names
controls by role, label, text, table cell and caption. Legacy web works through `frame_path` and custom
`table_cell` and `near_text` selector engines, which survive framesets and id-less tables. A desktop
surface would implement the same methods over Windows UI Automation or macOS Accessibility: role, label
and text map directly, table cells map to grid patterns, and coordinates plus a text check are the last
resort for Citrix. The artifact already declares `app.surface: legacy_web`, but the executor builds the
Playwright surface directly; a factory that dispatches on it is not built. The cost: anything without
semantic structure falls back to weaker coordinate locators.

**Reuse across tenants (built).** Artifacts belong to the vendor product (`mockbank_core >=2.0,<3.0`).
A tenant config supplies the base URL, credential variable names, PII mask selectors and an optional
override patch. Overrides may patch only `targets` and `conditions`, never the contract, so every tenant
gets the same API; the effective artifact is re-validated and its digest logged per run. Product-wide
errors live in a reviewed condition pack (`config/products/mockbank_core.conditions.yaml`) inherited by
every capability for that product. The trade-off: a tenant whose steps differ, not just its controls,
needs its own artifact. Overrides currently have a keying bug (Section 7).

**Drift per tenant and version.** A version outside the range is rejected in pre-flight, a fingerprint
mismatch fails the run, and fallback locators are logged per tenant and target. Flagging an
(artifact, tenant) pair for re-discovery from repeated unknown states or human fixes is design only.

## 5. Escalation & handoff

**Detecting stuck.** Replay escalates on the routing rule above, or on a condition whose handler is
`escalate`. Discovery escalates on its step budget (25), the same screen three times in a row, three
consecutive failed actions, the model calling `ask_human`, or an irreversible action. The request carries
the capability or goal, step and intent, reason, URL without its query string, the state needed to
resume, and a masked screenshot.

**Who is in control.** `SessionControl` is a state machine, one per live session:

```text
AUTOMATION → PAUSED → HUMAN → RESUMING → AUTOMATION
RESUMING → PAUSED (resync failed: ask again)      PAUSED | HUMAN → ABORTED (abort, timeout)
```

Automation calls `ensure_automation()` before every action, the human acts only in HUMAN, and nobody
acts in PAUSED or RESUMING. The operator page (127.0.0.1, random token per run) drives the transitions.
The human uses the same headed browser, context and cookies, so no state is copied. An input lock
enforces the rule in the browser: input is swallowed unless the state is HUMAN, so a stray click cannot
fix the page behind an unaware run. The human's actions are recorded, redacted (fills keep only length).

**Handing back.** Replay never trusts "I fixed it". It resumes after the furthest step whose checkpoint
holds on the live screen; if none holds, it retries a step whose action never ran, or asks again. Hand-
backs are capped at 3 per run, the run never continues past an irreversible step a human performed, and
with no operator an escalation fails at once. The cost: resync checks screens, not data (Section 7).
`evidence/human_handoff/` holds a real takeover that resynced and succeeded.

## 6. Safety

All data in the repo is synthetic. Evidence screenshots mask PII but may show bank-style partial values,
such as an SSN's last four digits.

**Allowlists.** Every request from every frame passes an origin allowlist (`context.route`), and every
action passes a `PolicyGate` (allowed action types plus risk), for the LLM and replay alike. Limit: it
covers origins, not paths.

**Safe versus irreversible.** The compiler classes each step `read`, `reversible` or `irreversible`. In
discovery an irreversible action needs a human, so the LLM never commits alone. In replay it needs the
caller's `--confirm` in pre-flight, and unattended runs need an approved artifact. Replay never stops
mid-transaction: a false stop costs a minute, a wrong commit costs a remediation. Limit: risk comes from
button names, and an unlisted label defaults to `read`, so the reviewer who approves the artifact is the
real safety net.

**Secrets and PII.** Credentials come from the environment through the session provider, never shown to
the LLM or logged. The LLM sees values only as shapes (`«shape:currency»`, `«masked»`), types only the
caller's parameters, may not press Enter, and receives page content escaped as untrusted. On write, PII
becomes a salted hash and secrets `«secret:name»`. Screenshots mask sensitive targets, tenant PII
selectors, text inputs and any leaf showing a digit. Limits: hashing is pseudonymisation, so member
numbers are only as safe as the salt; unlisted PII is masked only if it has a digit; and the leak test
(`tests/test_replay_mockbank.py`) scans test-run text evidence, not the committed `evidence/` or images.
A one-off scan before submission found none of the 53 raw member values in the committed run files;
the only matches were demo member ids in the hand-written `evidence/README.md` index. Screenshots were
checked by eye and show only the partial values above.

## 7. Cuts

**Deliberately cut.**

- **Operator console:** a minimal local page; remote browser streaming was out of scope.
- **Operator authentication:** the token blocks cross-site requests but identifies no one; production
  would use the bank's SSO.
- **Desktop surface:** design only; it would not test anything legacy web does not.
- **Also not built:** the surface factory, cross-run drift aggregation, and a MockBank flow that commits
  anything, so the irreversible gate is unit-tested but never exercised live.

Stretch goals touched: draft → approved gating for unattended replay, and per-tenant overrides (the
latter has the keying bug below). Not built: a capability tool interface, code generation, stability
scoring, and an assisted LLM fallback.

**Known gaps, in priority order.**

1. **Resync checks the screen, not the data** (design limitation). In the handoff evidence the human
   re-ran the search; a checkpoint cannot tell which member they typed. Next: verify the request's
   inputs on resume.
2. **Approval is not bound to content** (bug, not fixed). An approved file edited later stays approved.
   Next: store a content digest in `review` and check it in pre-flight.
3. **The risk classifier fails open** (design limitation). An irreversible button with an unlisted label
   is `read`. Next: default unknown clicks to `reversible` and add a path allowlist.
4. **Overrides are keyed by compiler-generated target ids** (bug, not fixed). `cu_beta` patches
   `member_id_field`, but the compiled artifact names it `member_number_field`, so the patched artifact
   fails validation and a `cu_beta` replay is rejected. It fails safe, but tenant reuse is not shown end
   to end. Next: key overrides by semantic description.
5. **Discovery's 300 s budget counts human time** (bug, not fixed). Expiry mid-handoff ends the run with
   an internal error. Next: pause the clock in HUMAN.

Smaller gaps include the lack of an overall run deadline (each step is bounded, the run is not).

**What I would build next.** I would expose the catalog as callable agent tools with a stability score
from repeated replays, so callers can see how reliable each capability is. Then I would add the surface
factory and a bounded, policy-checked LLM fallback for a single failed step.

This system was built with AI assistance (Claude Code); the reasoning behind non-obvious decisions is
recorded in module docstrings under `cua/`.
