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
cp .env.example .env       # then fill in ANTHROPIC_API_KEY and CUA_MODEL
```

| Variable | Needed for | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY`, `CUA_MODEL` | discovery only | Replay never calls the LLM |
| `MOCKBANK_USER`, `MOCKBANK_PASSWORD` | optional | Synthetic; default `teller01` / `mockbank-demo` for the local mock bank. Read only by the session provider |
| `CUA_REDACTION_SALT` | all runs | Salt for hashing PII in logs |
| `CUA_HEADLESS` | handoff | Must be `false` for a human to take over |

## Demo path

```bash
make mockbank              # terminal 1: target app on http://localhost:8000

make demo-discover         # terminal 2: real LLM run → capabilities/.../x.y.z.json (draft)
make demo-replay           # replay with a NEW member id → success (--supervised: artifact is still a draft)
make demo-notfound         # → business_outcome MEMBER_NOT_FOUND
make demo-handoff          # → escalation; open http://localhost:8001, take control, hand back
```

The demo targets run `--supervised` for now, so draft artifacts may replay. Don't run `cua approve` on the
hand-written fixture (`capabilities/mockbank/member.lookup_savings_balance/1.0.0.json`): it rewrites the file.
Approval for unattended replay comes back after Phase 4, on the generated artifact.

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
