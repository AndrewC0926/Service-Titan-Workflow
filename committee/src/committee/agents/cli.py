"""`committee agents ...` commands: dry-run, eval-harness, recall-probe.

By default every command uses recorded responses (no network). ``--live``
calls the Anthropic API with the pinned models, journals every call to the
real journal and is subject to the monthly budget guard.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from pathlib import Path
from typing import Any

import typer

from committee.agents.budget import BudgetGuard
from committee.agents.errors import AgentError
from committee.agents.fixtures import fixture_path, list_fixtures, load_fixture
from committee.agents.harness import run_harness
from committee.agents.llm import AnthropicClient, LLMClient, RecordedClient, RecordingClient
from committee.agents.prompts import PromptRegistry
from committee.agents.recall import ProbeItem, run_recall_probe
from committee.agents.runner import AGENT_ORDER, CommitteeResult, run_committee
from committee.agents.runtime import AgentRuntime
from committee.agents.schemas import AGENT_SCHEMAS
from committee.config.loader import ConfigError
from committee.context import AppContext
from committee.journal.store import Journal

app = typer.Typer(
    no_args_is_help=True, help="Agent committee: dry runs, evaluation, recall probes."
)

ROOT_OPT = typer.Option(None, "--root", help="Project root (defaults to nearest dir with config/).")
FIXTURES_OPT = typer.Option(
    None, "--fixtures-dir", help="Fixture directory (default: <root>/tests/fixtures/agents)."
)
LIVE_OPT = typer.Option(False, "--live", help="Call the real Anthropic API (costs money).")
RECORD_OPT = typer.Option(None, "--record", help="With --live: save responses to this JSON file.")
SAMPLES_OPT = typer.Option(
    None, "--samples", help="JSON file: {items: [...], recorded_response: {...}}."
)
ALPHA_OPT = typer.Option(0.05, "--alpha", help="Significance level for the binomial test.")
FIXTURE_OPT = typer.Option("fx_001", "--fixture", help="Fixture id, e.g. fx_001.")
JSON_OPT = typer.Option(False, "--json", help="Print the report summary as JSON.")


def _ctx(root: Path | None) -> AppContext:
    try:
        return AppContext.load(root)
    except ConfigError as e:
        typer.secho(str(e), fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2) from None


def _fixtures_dir(ctx: AppContext, d: Path | None) -> Path:
    return d if d is not None else ctx.root / "tests" / "fixtures" / "agents"


def _live_client(ctx: AppContext) -> AnthropicClient:
    key = ctx.secrets.get("ANTHROPIC_API_KEY")
    if not key:
        typer.secho("ANTHROPIC_API_KEY is not configured.", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    return AnthropicClient(key)


def _output_specs(registry: PromptRegistry) -> dict[str, str]:
    from committee.agents.schemas import output_spec

    return {a: output_spec(a, s, registry.schema) for a, s in AGENT_SCHEMAS.items()}


@app.command("dry-run")
def dry_run(
    fixture: str = FIXTURE_OPT,
    live: bool = LIVE_OPT,
    record: Path | None = RECORD_OPT,
    fixtures_dir: Path | None = FIXTURES_OPT,
    root: Path | None = ROOT_OPT,
) -> None:
    """Run all eleven agents on a fixture packet and print each agent's JSON."""
    ctx = _ctx(root)
    fx = load_fixture(fixture_path(_fixtures_dir(ctx, fixtures_dir), fixture))
    registry = PromptRegistry.load(ctx.root / "prompts")
    models = ctx.config.models
    client: LLMClient
    if live:
        journal = ctx.journal()
        client = _live_client(ctx)
        if record is not None:
            client = RecordingClient(client, record)
        budget: BudgetGuard | None = BudgetGuard(journal, models.budget)
        registry.register_trials(journal, _output_specs(registry))
    else:
        journal = Journal(":memory:")
        client = RecordedClient(fx.responses)
        budget = None
    run_id = f"dry-{fx.fixture_id}-{uuid.uuid4().hex[:8]}"
    rt = AgentRuntime(client, models, registry, run_id=run_id, journal=journal, budget=budget)
    result = CommitteeResult(review_id=fx.review.review_id)
    typer.echo(f"# {fx.fixture_id}: {fx.description}")
    typer.echo(
        f"# anon_id {fx.review.anon_id}; {len(fx.review.items)} evidence items; run {run_id}"
    )
    try:
        run_committee(rt, fx.review, fx.engines, result=result)
    except AgentError as e:
        typer.secho(f"NEEDS_ATTENTION: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    finally:
        for agent in AGENT_ORDER:
            if agent in result.results:
                res = result.results[agent]
                typer.echo(
                    f"\n## {agent}  model={res.model_id} attempts={res.attempts} "
                    f"cost=${res.cost_usd:.5f}"
                )
                typer.echo(json.dumps(res.output.model_dump(mode="json"), indent=2))
        journal.close()
    if result.gates is not None:
        typer.echo("\n## chair_gates (deterministic)")
        typer.echo(json.dumps(result.gates.model_dump(mode="json"), indent=2))
    typer.echo(f"\ntotal cost ${result.cost_usd:.5f}")


@app.command("eval-harness")
def eval_harness(
    fixtures_dir: Path | None = FIXTURES_OPT,
    as_json: bool = JSON_OPT,
    root: Path | None = ROOT_OPT,
) -> None:
    """Run all agents on the fixture packets; report validity, citations, probabilities."""
    ctx = _ctx(root)
    paths = list_fixtures(_fixtures_dir(ctx, fixtures_dir))
    if not paths:
        typer.secho("no fixtures found", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=2)
    registry = PromptRegistry.load(ctx.root / "prompts")
    report = run_harness([load_fixture(p) for p in paths], ctx.config.models, registry)
    summary = report.summary()
    if as_json:
        typer.echo(json.dumps(summary, indent=2))
        return
    for f in report.fixtures:
        typer.echo(f"{f.fixture_id}  {f.description}")
        for c in f.checks:
            cit = f"{c.citations.cited}/{c.citations.claims}" if c.citations else "-"
            typer.echo(
                f"   {c.agent:<19} {c.status:<9} attempts={c.attempts} citations={cit:<6} "
                f"prob_issues={len(c.probability_issues)} "
                f"contamination={'YES' if c.contamination else 'no'}"
            )
        if f.gates is not None:
            g = f.gates
            typer.echo(
                f"   gates: chair {g.chair_recommendation} {g.chair_size_pct_total:g}% -> "
                f"{g.recommendation} {g.size_pct_total:g}%"
                + (f"  ({'; '.join(g.violations)})" if g.violations else "")
            )
        if f.error:
            typer.echo(f"   error: {f.error}")
    typer.echo("")
    typer.echo(
        f"schema validity     {summary['schema_validity']:.1%} ({summary['repairs']} repaired)"
    )
    typer.echo(f"citation coverage   {summary['citation_coverage']:.1%}")
    typer.echo(f"probability issues  {len(summary['probability_failures'])}")
    for p in summary["probability_failures"]:
        typer.echo(f"   {p}")
    typer.echo(f"contamination flags {', '.join(summary['contamination_flags']) or 'none'}")


@app.command("recall-probe")
def recall_probe(
    samples: Path | None = SAMPLES_OPT,
    live: bool = LIVE_OPT,
    alpha: float = ALPHA_OPT,
    root: Path | None = ROOT_OPT,
) -> None:
    """Ask the model for past returns without context; flag recall above chance."""
    ctx = _ctx(root)
    path = samples or (_fixtures_dir(ctx, None) / "recall_samples.json")
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    items = [ProbeItem.model_validate(i) for i in data["items"]]
    models = ctx.config.models
    if live:
        journal = ctx.journal()
        client: LLMClient = _live_client(ctx)
        budget: BudgetGuard | None = BudgetGuard(journal, models.budget)
    else:
        journal = Journal(":memory:")
        client = RecordedClient({"recall_probe": [data["recorded_response"]]})
        budget = None
    run_id = f"probe-{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%S}"
    try:
        res = run_recall_probe(
            client, models, items, run_id=run_id, journal=journal, alpha=alpha, budget=budget
        )
    except AgentError as e:
        typer.secho(f"recall probe failed: {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    finally:
        journal.close()
    typer.echo(json.dumps(res.model_dump(mode="json"), indent=2))
    if res.contaminated:
        typer.secho(
            "CONTAMINATION: recall significantly above chance; quarantine this model cohort "
            "for the probed window.",
            fg=typer.colors.RED,
        )
