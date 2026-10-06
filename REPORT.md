# Design Report

<!-- Target: 1–3 pages. Use exactly these seven headings. Fill from DESIGN.md as you build. -->

## 1. Architecture
<!-- Diagram + components (Surface chokepoint, discovery, compiler, replay, handoff, evidence).
     Key decisions + trade-offs: single process + async (why), Python/Playwright/FastAPI (why),
     a11y tree as the primary observation (why), files on disk instead of DB (why). -->

## 2. Artifact schema
<!-- Contract vs implementation; targets by id; ranked semantic locators + MatchRule;
     checkpoints; declared outcomes; conditions map; templates-only values; risk + policy;
     app binding + provenance + review; semver rules. Show a trimmed example. -->

## 3. Determinism & error handling
<!-- Pre-flight → fingerprint → per-step: lease, policy, resolve, act, race (conditions then checkpoint,
     fixed precedence, polling, no sleeps, bounded retries) → extract + type-check → success check.
     Taxonomy table: business / recoverable / hard / rejected. UNKNOWN_STATE = never guess.
     Secondary: drift signals (fallback locator won, fingerprint mismatch). -->

## 4. Heterogeneity & multi-tenant
<!-- Surface seam: web → legacy web (frames, table_cell, near_text) → desktop (UIA/AX).
     Artifact bound to vendor product + version range; tenant overrides patch targets by id;
     effective-artifact hash per run; drift detection per tenant. -->

## 5. Escalation & handoff
<!-- Stuck detection (discovery + replay); intervention request contents; control lease + state machine;
     same live session; human action capture (redacted); resync to furthest satisfied checkpoint;
     abort/timeout. What is real vs mocked (operator UI, remote streaming). -->

## 6. Safety
<!-- Network-level allowlist; action allowlist; risk classes and handling (and why);
     credentials via session provider, never seen by LLM; redaction of logs/screenshots/artifacts;
     untrusted page content (prompt injection); limits of this model. -->

## 7. Cuts
<!-- What was deliberately left out or mocked, and why. What you would build next. -->
