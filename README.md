# CoreReplay

**Reliable AI-agent capabilities for legacy core banking systems without APIs.**

An LLM learns a back-office task once; every later run is a deterministic, audited replay, with a human in the loop when needed.

A computer-use automation system for legacy bank back-office apps that have **no API**.
The Python package and CLI are named `cua` (computer-use automation).

> **The model discovers. The artifact becomes a reusable capability. Deterministic replay is how an AI agent invokes it in production.**

```
goal ─► DISCOVERY (LLM, once) ─► COMPILE (no LLM) ─► capability artifact (JSON, versioned, reviewed)
                                                              │
        caller inputs ─────────────────────────────► REPLAY (no LLM) ─► success | business_outcome | failed | rejected
                                                              │
                                     stuck / unknown state ─► HUMAN HANDOFF (same live session) ─► resume
```

See [`REPORT.md`](REPORT.md) for the design write-up and [`evidence/`](evidence/) for recorded runs.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/).

```bash
make setup                 # uv sync + playwright install chromium
# then create .env (git-ignored) with the variables below; both the CLI and the mock bank read it
```

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY`, `CUA_MODEL` | discovery only | Replay never calls the LLM |
| `MOCKBANK_USER`, `MOCKBANK_PASSWORD` | every run | Required, no default: choose any values. The mock bank accepts exactly these (it refuses every login if they are unset) and the session provider signs in with them. Never logged or sent to the LLM |
| `CUA_REDACTION_SALT` | all runs | Salt for hashing PII in logs |
| `CUA_HEADLESS` | handoff | Must be `false` for a human to take over |

## Demo path

```bash
make mockbank              # terminal 1: target app on http://localhost:8000

make demo-discover         # terminal 2: real LLM run → capabilities/.../1.1.0.json (draft; VERSION=x.y.z to change)
make demo-replay           # replay with a NEW member id → success (--supervised: artifact is still a draft)
make demo-notfound         # → business_outcome MEMBER_NOT_FOUND
make demo-handoff          # → escalation; open the printed operator URL, take control,
                           #   click "Try Again" in the Chromium window, hand back → success
```

`capabilities/mockbank/member.lookup_savings_balance/1.0.0.json` is committed: it is the artifact from the
real discovery run in [`evidence/`](evidence/). Saved versions are immutable, so `make demo-discover` saves a
new version (`1.1.0` by default), and the replay targets use the newest one. **No API key?** Skip
`demo-discover`: the replay targets run the committed `1.0.0`.

The replay targets pass `--supervised` because artifacts are saved as drafts. Unattended replay needs
approval first: `uv run cua approve mockbank.member.lookup_savings_balance --reviewer <name>` (this rewrites
the artifact's `review` block in place).

Runs write their evidence to `evidence/_scratch/` (git-ignored); `cua replay --evidence-dir evidence` writes a
run you want to keep straight into [`evidence/`](evidence/).

**Operator page:** `cua replay --supervised` (or `--operator`) and `cua discover` serve it on
`127.0.0.1:8001` (`--operator-port`) for the length of the run and print its URL, e.g.
`http://127.0.0.1:8001/?token=…`. The token is random per run and required on every route (403
otherwise): CSRF protection, so another web page open in your browser cannot drive the API. It is not
authentication (see REPORT.md, Cuts). With an operator attached the browser is headed and an escalation
waits up to `limits.handoff_timeout_s` (900 s); unattended replays (no operator) fail at once.

**Running without live services:** replay needs no API key. A committed artifact plus the local mock bank are enough. `make test` runs everything offline.

## Fault injection

Append `--fault <name>` to `cua replay`. Available faults: `not_found`, `notice`, `slow`, `session_expired`, `denied`, `error`, `maint`.

## Project layout

See [`CLAUDE.md`](CLAUDE.md#layout).

## Tests

```bash
make test
make lint
```
