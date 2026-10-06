"""Predicate evaluation and the post-action race. Phase 3.

Fixed precedence, polled every limits.poll_interval_ms until step.expect.timeout_ms:
    1. watched conditions in declared order → first match → handler
         business_outcome → RunResult(business_outcome)
         recoverable      → dismiss / retry_step / reauthenticate (bounded) → re-enter race
         hard_failure     → fail | escalate
    2. step.expect satisfied (or no expect) → CONTINUE
timeout with nothing matched → UNKNOWN_STATE → snapshot + escalate. Never guess, never sleep blindly.
"""
