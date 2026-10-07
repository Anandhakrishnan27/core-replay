.PHONY: setup mockbank test lint schema demo-discover demo-replay demo-notfound demo-handoff

setup:            ## install deps + browser
	uv sync && uv run playwright install chromium

mockbank:         ## run the target app on :8000
	uv run uvicorn mockbank.app:app --port 8000 --reload

test:             ## run tests
	uv run pytest -q

lint:             ## lint, format check, types
	uv run ruff check . && uv run ruff format --check . && uv run mypy cua

schema:           ## regenerate capabilities/artifact.schema.json
	uv run python scripts/export_schema.py

# Saved versions are immutable and 1.1.0 (the committed discovery run) already exists: a new run saves
# as VERSION. Override with `make demo-discover VERSION=1.2.0`.
VERSION ?= 1.2.0

demo-discover:    ## real LLM discovery run (needs ANTHROPIC_API_KEY) → saves VERSION
	uv run cua discover --tenant cu_alpha --goal "look up member 10001 and read the savings balance" \
	  --capability-id mockbank.member.lookup_savings_balance --param member_id=10001 --version $(VERSION)

demo-replay:      ## deterministic replay, success
	uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=10002 --supervised

demo-notfound:    ## replay -> business outcome
	uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=99999 --supervised

demo-handoff:     ## replay -> escalation -> operator page on :8001 -> human fixes -> resync -> success
	@echo "Open the printed operator URL (it carries this run's token). When the run pauses:"
	@echo "  Take control -> click 'Try Again' in the Chromium window -> Hand back."
	uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=10001 --fault maint --supervised
