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
    async def observe(
        self, with_screenshot: bool = False
    ) -> Observation: ...  # a11y tree + refs (+ masked screenshot)
    async def resolve(
        self, target_id: str, target: Target
    ) -> Resolved: ...  # ranked locators + MatchRule → exactly one element
    async def act(
        self, action: Action, resolved: Resolved | None, risk: RiskClass, *, value: str | None
    ) -> None: ...  # policy-gated
    async def check(self, predicate: Predicate, targets: dict[str, Target]) -> bool: ...
    async def read(self, resolved: Resolved) -> str: ...
    async def snapshot(self, reason: str, *, dom: bool = False) -> list[str]: ...  # masked evidence paths
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
| **Recoverable** | system notice popup (dismiss), "Processing…" (wait until clear), session expired (reauthenticate once) | bounded recovery, log a `Recovery`, re-enter the race | continues. If exhausted → `RECOVERY_EXHAUSTED` (session: `SESSION_EXPIRED`) → escalate |
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
             recoverable → dismiss / wait_until_clear / reauthenticate (bounded) → re-enter race
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
| Redaction | `pii` → salted hash in logs; `secret` → `«secret:name»`; human-typed → length only; pre-flight errors never echo values |
| Screenshots and DOM dumps | Screenshots mask every element any locator of a `sensitive` target matches, plus the tenant's `mask_selectors` (PII on screen that is not a target, e.g. member name and number). DOM dumps drop form values, replace masked regions with `«masked»` and hash digit runs of 4+ digits. **Limit:** only listed regions are masked; PII elsewhere on a page (new fields after an app upgrade, free-text notes) is visible in screenshots until a tenant adds a selector. |
| Playwright traces | Off by default; explicit opt-in only; written to `evidence/_scratch/` (git-ignored), never committed. They contain unmasked page content, typed values and session cookies. Login and re-authentication run with tracing fully stopped, so credentials are not in them. |
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
| | Tenant overrides limited to `targets` / `conditions` | Free-form patches | The contract (API) must be identical for every tenant || 2026-10-06 | Mockbank faults are deterministic; `notice`, `session_expired`, `maint` fire once per browser context (`mb_once` cookie) | Random interstitials; server-side flags | Replay tests must not flake; a cookie scopes "once" to one Playwright context and survives re-login in it |
| 2026-10-06 | `session_expired` fires on opening the lookup form, not on the search POST | Expire on search | After reauthenticate, retrying the nav click works; retrying `submit_search` can't, because the form and the typed value are gone |
| 2026-10-06 | `maint` fires once and offers "Try Again" | Always on | The human in the handoff demo needs a way to fix the screen so resync can succeed |
| 2026-10-06 | Follow-up GETs (slow refresh, maint retry) carry an opaque single-use ticket, never the member number | Member number in the query string | No PII in URLs, so none in logs, history or evidence |
| 2026-10-06 | Notice renders inside `main`; an empty `frame_path` means "top document, then all frames, must match exactly once" (Phase 2 resolver) | Add `frame_path: ["main"]` to the fixture | Interstitials can appear in any frame; no fixture change needed |
| 2026-10-06 | Harness injects `--fault` by setting the `mb_fault` cookie on the browser context | Navigate to `/console?fault=x` | The artifact's `navigate` URL stays untouched |
| 2026-10-06 | Mockbank parses urlencoded forms with `urllib.parse` | Add `python-multipart` | No new dependency for two small legacy forms |
| 2026-10-06 | Ranked-locator loop and MatchRule live in `Surface.resolve()`; `replay/resolver.py` (Phase 3) only maps exceptions to `Failure` | Loop in the resolver | Resolution touches the browser, and only Surface may; discovery and resync reuse it |
| 2026-10-06 | `table_cell` / `near_text` are custom Playwright selector engines | Generated XPath | Column-index arithmetic and quote escaping in XPath 1.0 are fragile; engines give real lazy Locators (count, auto-wait, screenshot masks) |
| 2026-10-06 | `resolve()` and `check()` are single-shot snapshots (element handles, no waiting); waiting is `poll_until` in the caller | Waiting inside each call | One bounded wait in one place; the race in Phase 3 stays deterministic |
| 2026-10-06 | `url_matches` checks the top URL **and every frame URL** | Top URL only | In a frameset, an expired session shows `/login` inside `main` while the top URL stays `/console` |
| 2026-10-06 | `check()` returns False on transient errors (frame mid-navigation), for every predicate kind including `target_absent` | Raise / return True for absent | Never guess: the caller polls again |
| 2026-10-06 | `target_absent` = no locator finds any visible element; ambiguous is not absent | `not resolves()` | A text_pattern miss or a duplicate must not read as "no savings account" |
| 2026-10-06 | Network allowlist and tracing live on the context (`surface/browser.py`), not on Surface | Install from Surface | Must be in place before login's first request |
| 2026-10-06 | Tenant `mask_selectors` (CSS, all frames) for on-screen PII that is not a target; cu_alpha: `td.cap + td` | Mask whole frames; accept the leak | Keeps evidence useful while covering name and member number; the limit is documented in §7 |
| 2026-10-06 | Tracing off by default; `untraced()` fully stops tracing during login | Stop/start trace chunks | Found by test: the network recorder survives chunk boundaries and wrote the login POST body (password) into the next chunk |
| 2026-10-06 | Optional `SessionControl` on Surface; `act()`/`read()` call `ensure_automation()` when set | Only in the executor | The handoff check sits at the same chokepoint as policy, so nothing can bypass it |
| 2026-10-06 | Sign-on selectors are constants in `SessionProvider` (one provider per product) | In tenant config | Login is outside the artifact; a real deployment has one adapter per product or SSO |
| 2026-10-06 | `SCHEMA_VERSION: Final` annotation | Leave mypy failing | Newer mypy rejected `str` → `Literal["1.0"]`; type-only fix, generated JSON schema unchanged |
| 2026-10-06 | Handler `retry_step` renamed `wait_until_clear`: back off until the condition clears, then re-race; never repeat the action | Re-perform the step | "Processing" means the app already accepted the action; re-submitting could duplicate a write. Kept schema_version 1.0: no artifact other than the fixture exists yet |
| 2026-10-06 | A `dismiss` counts as done only when the condition has cleared (bounded by step.timeout_ms) | Re-race right after the click | Found by test: the race saw the old notice before the navigation committed and clicked a detached button |
| 2026-10-06 | Routing: escalate when a human at the screen could fix it (UNKNOWN_STATE, TARGET_*, RECOVERY_EXHAUSTED, SESSION_EXPIRED after failed re-auth, TIMEOUT, `escalate` handlers); fail when a click can't or mustn't (POLICY_VIOLATION, APP_VERSION_MISMATCH, CHECKPOINT_FAILED, `fail` handlers) | Escalate everything | A human must never override policy or bless a wrong read; principle documented in `executor.py` |
| 2026-10-06 | Tenant `product_version` vs `app.version_range` checked in pre-flight (`rejected`, no browser) via `packaging`; live fingerprint after login stays (`failed`) | Fingerprint only | A known-wrong version is refused without touching the app; `packaging` is the PEP 440 reference implementation |
| 2026-10-06 | Race deadline = `step.expect.timeout_ms`; a step without `expect` is one poll; `detect.timeout_ms` unused in the race | Per-condition deadlines | One deadline per step keeps precedence deterministic |
| 2026-10-06 | A condition using `target_absent` must still hold one poll interval later | Accept first match | A header painted before its table must not read as NO_SAVINGS_ACCOUNT |
| 2026-10-06 | Recovery budgets are per run, per condition | Per step | Strictly bounded however many steps a condition appears on |
| 2026-10-06 | On target-resolution timeout, classify against `applies_to: all` conditions before TARGET_NOT_FOUND | Report drift directly | A late popup or error page is not locator drift |
| 2026-10-06 | Decimal outputs returned as canonical strings (`"1203.55"`), dates as ISO strings | float | Money must stay exact; no schema change |
| 2026-10-06 | `result.json` in evidence has outputs redacted by sensitivity; the caller's RunResult keeps real values | Same file for both | Evidence must hold no raw PII (invariant 4) |
| 2026-10-06 | All templates resolved before the UI; `{{secrets.x}}` refused as unsupported | Env-var vault | No vault in this build; refuse rather than guess |
| 2026-10-06 | Until Phase 5 the CLI uses handoff timeout 0 (no operator); scratch runs go to `evidence/_scratch/` | Wait 900 s for nobody | Escalation path is real (SessionControl), just unattended |
| 2026-10-06 | Demo default sign-on `teller01` / `mockbank-demo` in `mockbank/app.py` and `cua/cli.py` when `MOCKBANK_USER` / `MOCKBANK_PASSWORD` are unset (env and `.env` still win) | Require `.env` | Runs out of the box. Synthetic credentials for a local mock only; the SessionProvider still reads only env, so a real deployment must set them |
