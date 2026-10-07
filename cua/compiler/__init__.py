"""Trace → CapabilityArtifact compiler. Deterministic: no LLM anywhere in this package."""


class CompileError(ValueError):
    """The trace cannot become a safe artifact. The message names the step and the reason, never values."""
