# Evidence

Curated demo runs, committed so reviewers can inspect them without running anything.

| Folder | What it shows |
|---|---|
| `discovery_<run_id>/` | The real LLM-driven run: `run.jsonl` (decisions + actions, redacted), step screenshots, `trace.json`, the produced artifact |
| `replay_<run_id>_success/` | Deterministic replay with a new input → `success` (+ any recoveries) |
| `replay_<run_id>_not_found/` | Replay → `business_outcome: MEMBER_NOT_FOUND` |
| `replay_<run_id>_handoff/` | Replay → unknown state → intervention → human takes over the same session → resume, with `human_actions` |

Each run folder contains:

- `run.jsonl`: one JSON event per line
- `result.json`: the `RunResult`
- `steps/NN_<step_id>.png`: masked screenshots
- `steps/NN_<reason>.dom.<frame>.html`: redacted DOM per frame, on failure only

Playwright traces are **not** committed. They are off by default and, when enabled with the explicit flag,
are written to `evidence/_scratch/traces/` (git-ignored), because they contain unmasked page content and
typed values.
