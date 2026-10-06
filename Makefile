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

demo-discover:    ## real LLM discovery run (needs ANTHROPIC_API_KEY)
	uv run cua discover --tenant cu_alpha --goal "look up member 10001 and read the savings balance"

demo-replay:      ## deterministic replay, success
	uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=10002

demo-notfound:    ## replay -> business outcome
	uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=99999

demo-handoff:     ## replay -> escalation -> operator page on :8001 -> resume
	uv run cua replay mockbank.member.lookup_savings_balance --tenant cu_alpha --input member_id=10001 --fault maint
