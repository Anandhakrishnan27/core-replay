"""Resolve a Target to exactly one element via ranked locators + MatchRule. Phase 3.

for i, locator in enumerate(target.locators):
    candidates = to_playwright(frame(target.frame_path), locator)
    count == 0 → next locator; count > 1 → remember ambiguity, next locator
    count == 1 → verify MatchRule (visible, enabled, text_pattern) → return Resolved(locator_index=i)
none → TARGET_AMBIGUOUS if any locator matched >1, else TARGET_NOT_FOUND
Log which locator won: index > 0 is a drift signal per (tenant, capability, target).
"""
