# Evidence

Curated runs, committed so reviewers can inspect them without running anything. Every file here is
redacted on write: PII is a salted hash (`«member_id:sha256:…»`), screenshots are masked, and no raw
member number, name, SSN, balance or account number appears in any log, result or DOM dump.

## Runs

One folder per kind of run; each holds one or more run folders (`discovery_<run_id>/`, `replay_<run_id>/`).

| Folder | Run | Input / fault | Result |
| --- | --- | --- | --- |
| `discovery/` | `discovery_20261007T042631Z_e4c4` | **Real LLM discovery** (claude-opus-5-5), goal "look up member 10001 and read the savings balance" | completed → compiled → self-test passed → saved as `capabilities/mockbank/member.lookup_savings_balance/1.0.0.json` |
| `discovery/` | `discovery_20261007T200029Z_e2dd` | **Real LLM discovery**, same goal, on the current mock bank UI (`--version 1.1.0`) | completed → compiled → self-test passed → saved as `…/1.1.0.json` (same steps and locators as 1.0.0) |
| `replay_success/` | `replay_20261007T192138Z_00de` | `member_id=10002` (a member discovery never saw) | `success`, `SUCCESS` (balance returned to the caller; hashed here) |
| `business_outcome/` | `replay_20261007T192139Z_36a1` | `member_id=99999` | `business_outcome`, `MEMBER_NOT_FOUND` |
| `business_outcome/` | `replay_20261007T192141Z_e1d5` | `member_id=10003` | `business_outcome`, `NO_SHARE_SAVINGS` (outcome derived by the compiler) |
| `recovery/` | `replay_20261007T192142Z_3321` | `--fault notice` | `success` after recovery `system_notice → dismiss` |
| `recovery/` | `replay_20261007T192144Z_db18` | `--fault session_expired` | `success` after recovery `session_expired → reauthenticate` |
| `hard_failure/` | `replay_20261007T192145Z_8be7` | `--fault denied` | `failed`, `PERMISSION_DENIED` (+ redacted DOM per frame) |
| `human_handoff/` | `replay_20261007T200748Z_065f` | `member_id=10001 --fault maint`, operator attached | unknown "Maintenance Window" → `UNKNOWN_STATE` escalation → `operator` takes control of the same live session → Try Again (then re-runs the search by hand) → hand back → resync to the `click_search_button` checkpoint → resumes at `read_savings_balance` → `success`; every human click, fill (`«redacted:len=5»`) and navigation in `handoffs[0].human_actions` |

Replays ran the committed `1.0.0` artifact against the local mock bank:

```bash
uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --supervised --no-operator \
  --evidence-dir evidence/<folder> <input / fault>
```

`--supervised` because the artifact is still a draft; `--no-operator` so nothing waits for a human. The
handoff run used `make demo-handoff` (operator attached, headed browser) and was moved here from
`evidence/_scratch/` unchanged, so the paths inside it say `_scratch`. While automation held control, the
browser ignored human input (input lock); the human acted only after Take control.

## What is in each folder

**Discovery** (`discovery/discovery_<run_id>/`):

- `run.jsonl`: one JSON event per line: LLM turns (tokens, stop reason), tool results, actions, compile, self-test, save
- `trace.json`: what the model did and why (element snapshots, verified locators, page states, short rationale); typed values and outputs hashed
- `steps/NN_step_NN_<tool>.png`: masked screenshot after each action
- `artifact.json`: the compiled draft (same content as the catalog's `1.0.0.json`)
- `selftest.json` + `replay_<run_id>/`: the compiler's self-test replay in a fresh browser context

Both discovery runs were written to `evidence/_scratch/` (the default) and moved here unchanged, so the
paths inside them say `evidence/_scratch/…`; `cua discover --evidence-dir evidence/discovery` now writes
straight here. The 1.0.0 run was recorded before the mock bank's teller-console restyle (earlier layout
and mask colour), and its `trace.json` predates the removal of two unused trace fields (`id_attr`,
`secret_name`, always `null`). Both are kept exactly as recorded.

**Replay** (`<kind>/replay_<run_id>/`):

- `run.jsonl`: one JSON event per line (step started, target resolved + locator index, race result, condition matched, recovery, escalation, finish)
- `result.json`: the `RunResult` (outputs redacted by sensitivity; the caller receives real values)
- `artifact.json`: the effective artifact that ran (after tenant overrides)
- `steps/NN_<step_id>.png`: masked screenshot after each step; `NN_<step_id>_<condition>.png` where a condition ended the run
- `steps/NN_<reason>.dom.<i>_<frame>.html`: redacted DOM per frame, on failure and escalation only

Screenshots mask sensitive targets, the tenant's `mask_selectors`, every text-entry control and every
other leaf showing a digit; reviewed chrome (status bar, field hints) stays readable. Some fields show a
partial value per the tenant's `screenshot_reveal` (e.g. SSN `•••-••-7731`); Date of Birth, Address, City
and balances stay fully masked.

Playwright traces are **not** committed: off by default, and when enabled (tests only) written to
`evidence/_scratch/traces/` (git-ignored), because they contain unmasked page content and typed values.
