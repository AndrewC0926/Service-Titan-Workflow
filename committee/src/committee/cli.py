"""`committee` command-line entry point."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import typer

from committee.config.control import pending_versions, sync
from committee.config.loader import ConfigError
from committee.context import AppContext
from committee.journal.anchor import check_anchors, write_anchor

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="Committee: AI investment committee."
)
config_app = typer.Typer(no_args_is_help=True, help="Configuration.")
journal_app = typer.Typer(no_args_is_help=True, help="Hash-chained journal.")
app.add_typer(config_app, name="config")
app.add_typer(journal_app, name="journal")

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


@config_app.command("pending")
def config_pending(root: Path | None = ROOT_OPT) -> None:
    """Journal any edits to controlled files and list changes not yet in effect."""
    ctx = load_ctx(root)
    now = dt.datetime.now(dt.UTC)
    with ctx.journal() as j:
        for r in sync(j, ctx.config_dir, now):
            typer.echo(
                f"{r.file}: {r.status}"
                + (f" (effective {r.effective_at:%Y-%m-%d %H:%M} UTC)" if r.effective_at else "")
            )
        for v in pending_versions(j, now):
            typer.echo(
                f"pending: {v.file} hash {v.content_hash[:12]} effective {v.effective_at:%Y-%m-%d %H:%M} UTC"
            )


@journal_app.command("verify")
def journal_verify(root: Path | None = ROOT_OPT) -> None:
    """Recompute every hash link. A break freezes order submission (P1)."""
    ctx = load_ctx(root)
    with ctx.journal() as j:
        rep = j.verify()
        anchor_problems = check_anchors(j, ctx.path(ctx.config.app.paths.anchor_dir))
        if rep.ok and not anchor_problems:
            typer.echo(
                f"journal OK: {rep.entries} entries, head seq {rep.last_seq} {rep.last_hash[:16]}"
            )
            return
        problems = rep.errors + anchor_problems
        for e in problems[:20]:
            typer.secho(f"  {e}", fg=typer.colors.RED, err=True)
        ctx.flags().set("frozen", f"journal verification failed: {problems[0]}")
        try:
            j.append(
                "incident",
                {"level": "P1", "kind": "journal_verify_failed", "errors": problems[:20]},
            )
        except Exception as exc:  # the store itself may be unusable
            typer.secho(f"  could not journal incident: {exc}", fg=typer.colors.RED, err=True)
        typer.secho(
            "P1: journal verification FAILED; order submission frozen",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


@journal_app.command("anchor")
def journal_anchor(root: Path | None = ROOT_OPT) -> None:
    """Write today's anchor (latest seq + hash) to the anchor directory."""
    ctx = load_ctx(root)
    with ctx.journal() as j:
        path = write_anchor(j, ctx.path(ctx.config.app.paths.anchor_dir))
    typer.echo(f"anchor written: {path}")


@journal_app.command("tail")
def journal_tail(n: int = 20, entry_type: str | None = None, root: Path | None = ROOT_OPT) -> None:
    """Show the last N entries."""
    ctx = load_ctx(root)
    with ctx.journal() as j:
        rows = list(j.entries(entry_type))[-n:]
    for e in rows:
        typer.echo(
            f"{e.seq:>6} {e.created_at_utc[:19]} {e.entry_type:<18} {e.hash[:12]} {json.dumps(e.payload)[:100]}"
        )


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
