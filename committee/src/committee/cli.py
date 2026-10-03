"""`committee` command-line entry point."""

from __future__ import annotations

from pathlib import Path

import typer

from committee.config.loader import ConfigError
from committee.context import AppContext

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Committee: AI investment committee."
)
config_app = typer.Typer(no_args_is_help=True, help="Configuration.")
app.add_typer(config_app, name="config")

ROOT_OPT = typer.Option(None, "--root", help="Project root (defaults to nearest dir with config/).")


def load_ctx(root: Path | None) -> AppContext:
    try:
        return AppContext.load(root)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


@config_app.command("check")
def config_check(root: Path | None = ROOT_OPT) -> None:
    """Validate all config files and print a summary with secrets masked."""
    ctx = load_ctx(root)
    c = ctx.config
    typer.echo(f"config OK  ({ctx.config_dir})")
    typer.echo(
        f"  profile: {c.risk_limits.profile}; satellite target {c.risk_limits.account.satellite_target_pct}%"
        f" (range {c.risk_limits.account.satellite_min_pct}-{c.risk_limits.account.satellite_max_pct}%)"
    )
    typer.echo(
        "  policy sleeves: "
        + ", ".join(f"{s.id}={s.target_pct:g}%" for s in c.policy_portfolio.sleeves)
    )
    typer.echo("  models: " + ", ".join(f"{k}={v.model}" for k, v in c.models.tiers.items()))
    typer.echo(f"  scenarios: {len(c.scenarios.scenarios)}; tax year {c.tax.effective_year}")
    typer.echo("  secrets:")
    for name, val in ctx.secrets.summary().items():
        typer.echo(f"    {name:22s} {val}")


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
