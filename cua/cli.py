"""CLI entry point: `cua discover | validate | approve | list | replay`."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from dotenv import load_dotenv
from pydantic import ValidationError

from cua import catalog

if TYPE_CHECKING:
    from cua.handoff.operator import OperatorServer

app = typer.Typer(no_args_is_help=True, add_completion=False)
load_dotenv()  # credentials (MOCKBANK_USER / MOCKBANK_PASSWORD) come only from env or .env: no defaults


OperatorFlag = Annotated[
    bool | None,
    typer.Option(
        "--operator/--no-operator",
        help="Serve the operator page so a human can take over on escalation "
        "(default: on for discover and --supervised replay, off for unattended replay)",
    ),
]
OperatorPort = Annotated[int, typer.Option(help="Operator page port (127.0.0.1 only)")]
Headless = Annotated[
    bool, typer.Option(envvar="CUA_HEADLESS", help="Hide the browser (a human can then not take over)")
]


@asynccontextmanager
async def _operator(attach: bool, port: int) -> AsyncIterator[OperatorServer | None]:
    """The in-process operator page for this run, or None. Its URL (with the per-run token) is printed
    to the terminal only: never logged, never in evidence."""
    if not attach:
        yield None
        return
    from cua.handoff.operator import operator_server

    async with operator_server(port=port) as op:
        typer.secho(f"operator page: {op.url}", fg="cyan", err=True)
        typer.secho("  (escalations wait here for a human; the token is this run's only)", err=True)
        yield op


def _operator_unavailable(e: Exception) -> None:
    typer.secho(f"refused  {e}", fg="yellow", err=True)
    raise typer.Exit(2)


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
    capability_id: Annotated[
        str, typer.Option(help="Id for the compiled artifact: <product>.<domain>.<verb_noun>")
    ],
    param: Annotated[
        list[str],
        typer.Option("--param", "-p", help="name=value for each value the goal mentions; repeatable"),
    ] = [],  # noqa: B006
    tenant: Annotated[str, typer.Option(help="Tenant id from config/tenants")] = "cu_alpha",
    version: Annotated[str, typer.Option(help="Semver of the new artifact")] = "1.0.0",
    headless: Headless = False,
    fault: Annotated[str | None, typer.Option(help="Mock bank fault to inject during discovery")] = None,
    operator: OperatorFlag = None,
    operator_port: OperatorPort = 8001,
    evidence_dir: Annotated[
        Path | None,
        typer.Option(help="Parent folder for this run's evidence (default: evidence/_scratch, git-ignored)"),
    ] = None,
) -> None:
    """Real LLM-driven run → trace → compiled draft → self-test replay → catalog (only if it passed).

    Exit code: 0 saved, 1 discovery / compile / self-test failed, 2 refused before starting.
    """
    from cua.config import load_policy, load_tenant
    from cua.discovery.agent import SCRATCH_RUNS, default_messages_api, default_model
    from cua.discovery.pipeline import Discovered, discover_capability
    from cua.handoff.operator import OperatorUnavailable

    async def run() -> Discovered:
        async with _operator(operator is not False, operator_port) as op:
            return await discover_capability(
                goal,
                load_tenant(tenant),
                load_policy(),
                _parse_inputs(param),
                capability_id,
                version,
                messages_api=default_messages_api(),
                model=default_model(),
                headed=not headless,
                fault=fault,
                operator=op,
                evidence_root=evidence_dir or SCRATCH_RUNS,
            )

    try:
        result = asyncio.run(run())
    except OperatorUnavailable as e:
        _operator_unavailable(e)
    typer.echo(json.dumps(result.summary(), indent=2))
    color = {"saved": "green", "refused": "yellow"}.get(result.status, "red")
    typer.secho(f"{result.status}  {result.reason}", fg=color, err=True)
    raise typer.Exit({"saved": 0, "refused": 2}.get(result.status, 1))


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
    operator: OperatorFlag = None,
    operator_port: OperatorPort = 8001,
    headless: Headless = False,
    evidence_dir: Annotated[
        Path | None,
        typer.Option(help="Parent folder for this run's evidence (default: evidence/_scratch, git-ignored)"),
    ] = None,
) -> None:
    """Deterministic replay (no LLM). Prints the RunResult JSON.

    With an operator (--supervised, or --operator) an escalation waits up to the policy's handoff timeout
    for a human in the same (headed) browser; without one it ends the run at once.
    Exit code: 0 success or business outcome (not an error), 1 failed, 2 rejected.
    """
    from cua.handoff.operator import OperatorUnavailable
    from cua.replay.executor import SCRATCH_RUNS
    from cua.replay.executor import replay as run_replay
    from cua.schema.result import RunResult, RunStatus

    attach = supervised if operator is None else operator

    async def run() -> RunResult:
        async with _operator(attach, operator_port) as op:
            return await run_replay(
                capability_id,
                tenant,
                _parse_inputs(input),
                version=version,
                mode="supervised" if supervised else "unattended",
                confirmed=confirm,
                fault=fault,
                operator=op,
                headed=op is not None and not headless,  # the human uses this very window
                evidence_root=evidence_dir or SCRATCH_RUNS,
            )

    try:
        result = asyncio.run(run())
    except OperatorUnavailable as e:
        _operator_unavailable(e)
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
