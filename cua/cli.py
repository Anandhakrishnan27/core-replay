"""CLI entry point: `cua discover | compile | validate | approve | list | replay`."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from cua import catalog

app = typer.Typer(no_args_is_help=True, add_completion=False)
load_dotenv()
# Demo-only defaults for the synthetic mock bank (must match mockbank/app.py). Env and .env win.
# Real deployments set credentials in the environment; the SessionProvider still reads only env.
os.environ.setdefault("MOCKBANK_USER", "teller01")
os.environ.setdefault("MOCKBANK_PASSWORD", "mockbank-demo")


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"expected key=value, got {p!r}")
        k, v = p.split("=", 1)
        out[k] = v
    return out


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural-language goal")],
    tenant: Annotated[str, typer.Option(help="Tenant id from config/tenants")] = "cu_alpha",
    capability_id: Annotated[str | None, typer.Option(help="Id for the compiled artifact")] = None,
) -> None:
    """Real LLM-driven run against the live app → trace → compiled draft artifact."""
    from cua.discovery.agent import run_discovery

    asyncio.run(run_discovery(goal, tenant))


@app.command()
def validate(path: Path) -> None:
    """Validate an artifact file against the schema."""
    try:
        art = catalog.load_file(path)
    except ValidationError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(1) from e
    typer.secho(
        f"OK  {art.id}@{art.version}  ({art.review.status.value}, max_risk={art.policy.max_risk.value})",
        fg="green",
    )


@app.command()
def approve(capability_id: str, reviewer: Annotated[str, typer.Option()], version: str | None = None) -> None:
    """Mark an artifact approved (required for unattended replay)."""
    path = catalog.approve(capability_id, reviewer, version)
    typer.echo(f"approved → {path}")


@app.command("list")
def list_capabilities() -> None:
    """List capabilities in the catalog."""
    for row in catalog.list_all():
        typer.echo(f"{row['id']}@{row['version']}  [{row['status']}]  {row['title']}")


@app.command()
def replay(
    capability_id: str,
    tenant: Annotated[str, typer.Option()] = "cu_alpha",
    input: Annotated[list[str], typer.Option("--input", "-i", help="key=value, repeatable")] = [],  # noqa: B006
    version: str | None = None,
    supervised: Annotated[bool, typer.Option(help="Allow draft artifacts; human may be pulled in")] = False,
    confirm: Annotated[bool, typer.Option(help="Confirm irreversible capability")] = False,
    fault: Annotated[str | None, typer.Option(help="Mock bank fault to inject")] = None,
) -> None:
    """Deterministic replay (no LLM). Prints the RunResult JSON.

    Exit code: 0 success or business outcome (not an error), 1 failed, 2 rejected.
    """
    from cua.replay.executor import replay as run_replay
    from cua.schema.result import RunStatus

    result = asyncio.run(
        run_replay(
            capability_id,
            tenant,
            _parse_inputs(input),
            version=version,
            mode="supervised" if supervised else "unattended",
            confirmed=confirm,
            fault=fault,
            # TODO(phase-5): start the operator on :8001 and use policy.limits.handoff_timeout_s.
            # Until then no operator is attached, so an escalation ends at once as `timed_out`.
            handoff_timeout_s=0,
        )
    )
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2))
    detail = result.outcome_code or (result.failure.category.value if result.failure else "")
    color = {
        RunStatus.success: "green",
        RunStatus.business_outcome: "cyan",
        RunStatus.failed: "red",
        RunStatus.rejected: "yellow",
    }[result.status]
    typer.secho(f"{result.status.value}  {detail}  evidence: {result.evidence_dir}", fg=color, err=True)
    codes = {RunStatus.success: 0, RunStatus.business_outcome: 0, RunStatus.failed: 1, RunStatus.rejected: 2}
    raise typer.Exit(codes[result.status])


if __name__ == "__main__":
    app()
