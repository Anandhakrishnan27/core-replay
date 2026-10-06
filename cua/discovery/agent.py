"""Discovery agent: LLM-driven observe → decide → act loop. Phase 4. The ONLY place an LLM decides.

Loop (bounded by limits.discovery_max_steps / discovery_timeout_s):
    obs    = await observe(surface, with_screenshot=needs_visual)
    reply  = anthropic.messages.create(model, system=SYSTEM_PROMPT, tools=TOOLS, messages=history + user_turn)
    call   = reply's tool_use block
    done       → finish(outputs) → trace.status="completed"
    ask_human  → handoff (same session) → append human actions to history → continue
    otherwise  → surface.act(...) (policy-gated; NeedsHuman → handoff) → recorder.add(TraceAction(...))
    stuck.record(...) → reason → handoff → continue, or abort → trace.status="escalated"
Then: save trace.json → compiler.
"""

from __future__ import annotations

from cua.schema.trace import DiscoveryTrace


async def run_discovery(goal: str, tenant_id: str, *, headless: bool = False) -> DiscoveryTrace:
    raise NotImplementedError("phase-4: discovery loop")
