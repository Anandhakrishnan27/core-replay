"""Tool definitions exposed to the LLM. Every call is executed through Surface (policy-gated)."""

from __future__ import annotations

from typing import Any

_REF = {"type": "string", "description": "Element ref from the snapshot, e.g. e14"}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "click",
        "description": "Click an element.",
        "input_schema": {"type": "object", "properties": {"ref": _REF}, "required": ["ref"]},
    },
    {
        "name": "fill",
        "description": "Type a value into a text field. Use only values given in the goal.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF, "value": {"type": "string"}},
            "required": ["ref", "value"],
        },
    },
    {
        "name": "select",
        "description": "Choose an option in a dropdown.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF, "option": {"type": "string"}},
            "required": ["ref", "option"],
        },
    },
    {
        "name": "press",
        "description": "Press a key, e.g. Enter.",
        "input_schema": {
            "type": "object",
            "properties": {"key": {"type": "string"}, "ref": _REF},
            "required": ["key"],
        },
    },
    {
        "name": "extract",
        "description": "Mark the element whose text is a value the goal asks for.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": _REF, "name": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}},
            "required": ["ref", "name"],
        },
    },
    {
        "name": "done",
        "description": "The goal is complete.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string"}},
            "required": ["summary"],
        },
    },
    {
        "name": "ask_human",
        "description": "You are stuck or a human decision is required.",
        "input_schema": {
            "type": "object",
            "properties": {"reason": {"type": "string"}},
            "required": ["reason"],
        },
    },
]
