"""`committee scenarios` sub-commands (registered by the main CLI)."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from committee.config.loader import ConfigError, default_root, load_config
from committee.engines.scenario.engine import run_scenarios
from committee.engines.scenario.holdings_io import demo_portfolio, read_holdings_csv
from committee.engines.scenario.models import ScenarioReport

app = typer.Typer(no_args_is_help=True, help="Scenario engine: losses and risk-budget modifier.")

HOLDINGS_OPT = typer.Option(
    None, "--holdings", help="Holdings CSV (default: demo portfolio from policy_portfolio.yaml)."
)
ROOT_OPT = typer.Option(None, "--root", help="Project root (defaults to nearest dir with config/).")
TOTAL_OPT = typer.Option(
    None, "--total-value", help="Total account value incl. cash (default: sum of holdings)."
)
JSON_OPT = typer.Option(False, "--json", help="Print the full report as JSON.")


@app.callback()
def _group() -> None:
    """Scenario engine commands."""


def format_report(report: ScenarioReport) -> str:
    lines = [
        f"{'scenario':<26}{'prob':>6}{'total':>10}{'satellite':>11}  budget",
    ]
    for loss in report.losses:
        flag = "BREACH" if loss.breaches_budget else "ok"
        lines.append(
            f"{loss.name:<26}{loss.probability:>6.0%}{-loss.total_return:>10.1%}"
            f"{-loss.satellite_return:>11.1%}  {flag}"
        )
    lines.append("(losses shown positive; negative = gain)")
    lines.append(
        f"total value ${report.total_value:,.0f}; satellite ${report.satellite_value:,.0f}; "
        f"satellite expected vol {report.satellite_expected_vol:.1%}; "
        f"loss threshold {report.loss_threshold:.1%}"
    )
    lines.append(f"risk-budget modifier: {report.modifier:.2f}")
    lines.extend(f"  - {d.text}" for d in report.drivers)
    return "\n".join(lines)


@app.command("run")
def run(
    holdings: Path | None = HOLDINGS_OPT,
    root: Path | None = ROOT_OPT,
    total_value: float | None = TOTAL_OPT,
    as_json: bool = JSON_OPT,
) -> None:
    """Print portfolio losses per scenario and the risk-budget modifier."""
    config_dir = (root or default_root()) / "config"
    try:
        cfg = load_config(config_dir)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    if holdings is None:
        positions = demo_portfolio(cfg.policy_portfolio)
        source = "demo portfolio (policy_portfolio.yaml + placeholder satellite)"
    else:
        try:
            positions = read_holdings_csv(holdings)
        except (OSError, ValueError) as e:
            typer.secho(f"cannot read holdings: {e}", fg=typer.colors.RED, err=True)
            raise typer.Exit(code=2) from None
        source = str(holdings)
    try:
        report = run_scenarios(
            positions, cfg.scenarios, policy=cfg.policy_portfolio, total_value=total_value
        )
    except ValueError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None
    if as_json:
        typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
        return
    typer.echo(f"holdings: {source}")
    typer.echo(format_report(report))
