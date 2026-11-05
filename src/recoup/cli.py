"""Command line entry point: `recoup --help`."""

from __future__ import annotations

import json
from pathlib import Path

import typer

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)

DATASETS_DIR = Path("datasets")


@app.callback()
def main() -> None:
    """Recoup: payer-contract underpayment audits with clause-level citations."""


@app.command()
def synth(
    name: str = typer.Argument("all", help="smoke, full, adversarial, holdout or all"),
    out: Path = typer.Option(DATASETS_DIR, help="parent directory for the dataset folders"),
) -> None:
    """Generate synthetic contracts, claims, 835 remits and ground truth."""
    from recoup.synth.datasets import SPECS, build

    names = list(SPECS) if name == "all" else [name]
    for dataset in names:
        if dataset not in SPECS:
            raise typer.BadParameter(f"unknown dataset {dataset!r}; choose from {', '.join(SPECS)}")
        manifest = build(SPECS[dataset], out / dataset)
        typer.echo(json.dumps(manifest))


@app.command()
def ingest(
    pdf: Path,
    extractor: str = typer.Option(None, help="rules (default, $0) or claude (needs ANTHROPIC_API_KEY)"),
) -> None:
    """Extract cited terms from one contract PDF and print the result as JSON."""
    from recoup.ingest.pipeline import get_extractor, ingest_pdf

    result = ingest_pdf(pdf.read_bytes(), pdf.name, extractor=get_extractor(extractor))
    typer.echo(result.model_dump_json(indent=2))


@app.command()
def audit(
    folder: Path = typer.Argument(..., help="a folder with contracts/*.pdf, remits/*.835 and claims.csv"),
    out: Path = typer.Option(None, help="write every finding to this JSON file"),
    extractor: str = typer.Option(None, help="rules (default) or claude"),
) -> None:
    """Ingest the contracts, audit the remits, print a summary."""
    from recoup.audit.pipeline import audit_folder
    from recoup.ingest.pipeline import get_extractor, ingest_pdf

    chosen = get_extractor(extractor)
    contracts = [
        ingest_pdf(p.read_bytes(), p.name, extractor=chosen)
        for p in sorted((folder / "contracts").glob("*.pdf"))
    ]
    findings, summary = audit_folder(folder, contracts)
    for contract in contracts:
        typer.echo(
            f"{contract.filename}: {contract.status.value}"
            + (f" ({contract.issues[0]})" if contract.issues else "")
        )
    typer.echo(summary.model_dump_json(indent=2))
    if out:
        out.write_text(json.dumps([f.model_dump(mode="json") for f in findings], indent=2))
        typer.echo(f"wrote {len(findings)} findings to {out}")


@app.command(name="eval")
def eval_command(
    names: list[str] = typer.Argument(None, help="datasets to score (default: all four)"),
    root: Path = typer.Option(DATASETS_DIR, help="where the dataset folders live"),
    extractor: str = typer.Option(None, help="rules (default) or claude"),
    baseline: Path = typer.Option(Path("evals/baseline.json"), help="committed baseline metrics"),
    gate: bool = typer.Option(False, help="exit 1 if any metric regresses against the baseline"),
    bless: bool = typer.Option(
        False, "--bless", help="record this run as the baseline; safety metrics still gate"
    ),
    report_only: bool = typer.Option(False, "--report-only", help="gate on safety metrics only"),
    markdown_out: Path = typer.Option(None, "--markdown", help="write the PR-comment table here"),
    report_out: Path = typer.Option(None, "--report", help="write full metrics JSON here"),
) -> None:
    """Run ingest + audit on each dataset and score it against ground truth."""
    from recoup.evals import gate as gating
    from recoup.evals.metrics import evaluate
    from recoup.ingest.pipeline import get_extractor

    chosen = get_extractor(extractor)
    current = {
        name: evaluate(root / name, chosen) for name in (names or ["smoke", "full", "adversarial", "holdout"])
    }
    changes = gating.compare(current, gating.load_baseline(baseline), safety_only=bless or report_only)
    table = gating.markdown(current, changes, chosen.name)
    typer.echo(table)
    if markdown_out:
        markdown_out.write_text(table)
    if report_out:
        report_out.write_text(json.dumps({k: v.model_dump() for k, v in current.items()}, indent=2))
    if (gate or bless) and any(c.regression for c in changes):
        raise typer.Exit(1)
    if bless:
        baseline.parent.mkdir(parents=True, exist_ok=True)
        gating.write_baseline(baseline, current)
        typer.echo(f"baseline written to {baseline}")


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run the API (uses the mocks at RECOUP_MOCKS_URL for the outside world)."""
    import uvicorn
    from fastapi import FastAPI

    from recoup import runtime
    from recoup.api.app import create_app

    service, clearinghouse = runtime.build()
    api = create_app(service, runtime.api_keys(), runtime.env("RECOUP_WEBHOOK_SECRET", "whsec-dev"), clearinghouse,
                     eval_report=runtime.eval_report())  # fmt: skip
    prefix = runtime.env("RECOUP_ROOT_PATH", "")
    if prefix:  # behind a load balancer that routes /api/* here without stripping the prefix
        outer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        outer.mount(prefix, api)
        uvicorn.run(outer, host=host, port=port, proxy_headers=True)
    else:
        uvicorn.run(api, host=host, port=port)


@app.command()
def mocks(
    host: str = "127.0.0.1",
    port: int = 8001,
    eras: Path = typer.Option(Path("datasets/full/remits"), help="835 files the clearinghouse serves"),
    callback: str = typer.Option("http://127.0.0.1:8000/webhooks/payer", help="where payer webhooks go"),
) -> None:
    """Run the mock clearinghouse, payer portal and PMS."""
    import uvicorn

    from recoup import runtime
    from recoup.mocks.app import MockState, create_mock_app

    # The mocks must share the API's secrets, or every webhook signature and token check fails.
    pms_id, _, pms_secret = runtime.env("RECOUP_PMS_CLIENT", "recoup:pms-secret").partition(":")
    state = MockState(era_dir=eras, callback_url=callback, pms_client=(pms_id, pms_secret),
                      webhook_secret=runtime.env("RECOUP_WEBHOOK_SECRET", "whsec-dev"))  # fmt: skip
    uvicorn.run(create_mock_app(state), host=host, port=port)


@app.command()
def worker() -> None:
    """Drain the job queue forever."""
    from recoup import runtime
    from recoup.worker import run_forever

    service, clearinghouse = runtime.build()
    run_forever(service, clearinghouse)


@app.command(name="db-bootstrap")
def db_bootstrap(
    app_role: str = "recoup_app",
    seed_dataset: Path = typer.Option(
        None, "--seed", help="load this dataset if the database has no contracts"
    ),
) -> None:
    """Run migrations as the admin URL, then create or update the least-privilege app role.

    The app must connect as a role with neither SUPERUSER nor BYPASSRLS: either one switches
    row-level security off without an error. APP_DB_PASSWORD sets that role's password.
    """
    import os
    import subprocess

    from sqlalchemy import text

    from recoup.db.session import Database

    subprocess.run(["alembic", "upgrade", "head"], check=True)
    db = Database()
    if not db.is_postgres:
        typer.echo("not Postgres: migrations applied, no role to create")
        return
    password = os.environ["APP_DB_PASSWORD"]
    with db.engine.begin() as connection:
        exists = connection.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": app_role}
        ).first()
        verb = "ALTER" if exists else "CREATE"
        quoted = password.replace("'", "''")
        connection.execute(text(f"{verb} ROLE {app_role} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{quoted}'"))
        connection.execute(
            text(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_role}")
        )
        connection.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {app_role}"))
    typer.echo(f"migrations applied; {app_role} ready")
    if seed_dataset is not None:
        from recoup import runtime

        service, _ = runtime.build()
        if service.list_documents("demo"):
            typer.echo("already seeded")
        else:
            _seed(service, seed_dataset, "demo")


@app.command()
def mcp(tenant: str = "demo") -> None:
    """Serve the MCP tools over stdio."""
    from recoup import runtime
    from recoup.mcp_server import create_mcp

    service, _ = runtime.build()
    create_mcp(service, tenant).run()


@app.command()
def seed(
    dataset: Path = typer.Argument(Path("datasets/full")),
    tenant: str = "demo",
) -> None:
    """Load a dataset's contracts, claims and remits into the database, as a practice would on day one."""
    from recoup import runtime

    service, _ = runtime.build()
    _seed(service, dataset, tenant)
    typer.echo(json.dumps(service.summary(tenant), indent=2))


def _seed(service: object, dataset: Path, tenant: str) -> None:
    from recoup.service import Recoup

    assert isinstance(service, Recoup)
    for pdf in sorted((dataset / "contracts").glob("*.pdf")):
        result = service.ingest_contract(tenant, pdf.read_bytes(), pdf.name, "seed")
        typer.echo(f"{pdf.name}: {result['status']}")
    typer.echo(f"claims: {service.load_claims(tenant, (dataset / 'claims.csv').read_text(), 'seed')}")
    for remit in sorted((dataset / "remits").glob("*.835")):
        service.ingest_remit(tenant, remit.read_text(), f"seed:{remit.name}", "seed")


if __name__ == "__main__":
    app()
