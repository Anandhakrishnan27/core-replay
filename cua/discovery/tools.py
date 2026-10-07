"""Tool definitions exposed to the LLM. Every call is executed through Surface (policy-gated).

All tools are `strict: true` (schema-valid inputs guaranteed), so every object schema sets
`additionalProperties: false`. String `pattern`s are deliberately left out: the agent validates names
itself (cua.discovery.agent) rather than depend on which JSON Schema keywords strict mode enforces.
The list is fixed for the whole run: changing tools mid-run would invalidate the cached prefix.
"""

from __future__ import annotations

from typing import Any

_REF = {"type": "string", "description": "Element ref from the snapshot, e.g. f3e14"}


def _tool(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


TOOLS: list[dict[str, Any]] = [
    _tool("click", "Click an element.", {"ref": _REF}, ["ref"]),
    _tool(
        "fill",
        "Type a value into a text field. Only values given in the goal are accepted.",
        {"ref": _REF, "value": {"type": "string"}},
        ["ref", "value"],
    ),
    _tool(
        "select",
        "Choose an option in a dropdown.",
        {"ref": _REF, "option": {"type": "string"}},
        ["ref", "option"],
    ),
    _tool(
        "press",
        "Press a key, e.g. Tab or Escape. To submit a form, click its button instead of pressing Enter.",
        {"key": {"type": "string"}, "ref": _REF},
        ["key"],
    ),
    _tool(
        "dismiss",
        "Close an unexpected dialog or notice that blocks the task (it is not part of the task itself). "
        "Give the ref of the control that closes it, e.g. its OK button.",
        {"ref": _REF},
        ["ref"],
    ),
    _tool(
        "extract",
        "Mark the element whose text is a value the goal asks for. "
        "`name` is snake_case, e.g. savings_balance. The value itself stays hidden from you.",
        {"ref": _REF, "name": {"type": "string"}},
        ["ref", "name"],
    ),
    _tool("done", "The goal is complete.", {"summary": {"type": "string"}}, ["summary"]),
    _tool(
        "ask_human",
        "You are stuck or a human decision is required.",
        {"reason": {"type": "string"}},
        ["reason"],
    ),
]
