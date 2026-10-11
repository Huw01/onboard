"""Command-line interface: `onboard generate`, `onboard corrupt`."""

from __future__ import annotations

from pathlib import Path

import typer

app = typer.Typer(help="LLM-assisted data onboarding for enterprise systems.", no_args_is_help=True)


@app.command()
def generate(
    out: Path = typer.Option(Path("data/clean"), help="Output directory for the clean CSVs."),
    seed: int = typer.Option(42, help="Random seed."),
) -> None:
    """Generate the clean, target-schema CMMS dataset."""
    from onboard.generate import generate as gen, write_tables

    tables = gen(seed)
    write_tables(tables, out)
    for name, df in tables.items():
        typer.echo(f"{name:<14} {len(df):>6} rows")
    typer.echo(f"wrote {out}/")


@app.command()
def corrupt(
    clean: Path = typer.Option(Path("data/clean"), help="Clean CSVs to start from."),
    legacy: Path = typer.Option(Path("data/legacy"), help="Where to write the legacy export."),
    truth: Path = typer.Option(Path("data/ground_truth"), help="Where to write the answer key."),
    seed: int = typer.Option(42, help="Random seed."),
) -> None:
    """Corrupt the clean dataset into a legacy export plus a ground-truth answer key."""
    from onboard.corrupt import corrupt as run

    c = run(clean, legacy, truth, seed)
    for name, s in c.sheets.items():
        typer.echo(f"{name:<12} {len(s.df):>6} rows")
    counts = c.corruptions().corruption_type.value_counts().sort_index()
    typer.echo("corruptions:")
    for kind, n in counts.items():
        typer.echo(f"  {kind:<22} {n:>5}")
    typer.echo(f"  {'TOTAL':<22} {counts.sum():>5}")
    typer.echo(f"wrote {legacy / 'legacy_export.xlsx'} and {truth}/corruptions.csv")


@app.command()
def validate(
    export: Path = typer.Argument(Path("data/legacy/legacy_export.xlsx"),
                                  help="Legacy export (.xlsx with one sheet per table)."),
    mapping: Path = typer.Option(Path("mappings/legacy_cmms.json"), help="Column/code mapping."),
    out: Path = typer.Option(Path("out"), help="Output directory."),
    as_of: str = typer.Option(None, help="Extract date (YYYY-MM-DD); later dates are flagged."),
    truth: Path = typer.Option(None, help="Answer key directory, to score the run."),
) -> None:
    """Validate a legacy export: clean CSVs, exceptions.csv and a run summary."""
    from datetime import date

    from onboard.corrupt import read_legacy_xlsx
    from onboard.mapping import load_mapping
    from onboard.validate import validate as run, write_result

    result = run(read_legacy_xlsx(export), load_mapping(mapping),
                 date.fromisoformat(as_of) if as_of else None)
    write_result(result, out)
    typer.echo(result.summary_text())
    typer.echo(f"wrote {out}/clean/, {out}/exceptions.csv, {out}/summary.txt")
    if truth:
        from onboard.score import score

        for k, v in score(result, truth).as_dict().items():
            typer.echo(f"{k:<20} {v}")


@app.command()
def run(
    export: Path = typer.Argument(..., help="Legacy export (.xlsx, one sheet per table)."),
    out: Path = typer.Option(Path("out"), help="Output directory."),
    as_of: str = typer.Option(None, help="Extract date (YYYY-MM-DD); later dates are flagged."),
    mapping: Path = typer.Option(None, help="Use this mapping instead of asking the LLM."),
    review: bool = typer.Option(True, help="Run the LLM review pass on unresolved rows."),
    llm_mode: str = typer.Option(None, help="replay | auto | record | live (default: auto)."),
    model: str = typer.Option(None, help="Model name (default: grok-4.7)."),
    truth: Path = typer.Option(None, help="Answer key directory, to score the run."),
) -> None:
    """Map, validate and review a legacy export end to end."""
    from datetime import date

    from onboard.llm import LLM
    from onboard.pipeline import run as run_pipeline, write_run

    llm = LLM(model=model, mode=llm_mode, usage_path=out / "usage.csv")
    rr = run_pipeline(export, llm, date.fromisoformat(as_of) if as_of else None,
                      mapping, review, progress=_print_progress)
    write_run(rr, out)
    typer.echo(rr.after.summary_text())
    acc = sum(d["accepted"] for d in rr.decisions)
    typer.echo(f"LLM review: {len(rr.decisions)} questions, {acc} answers accepted")
    u = rr.usage
    typer.echo(f"LLM usage: {u['calls']} calls, {u['prompt_tokens']} in "
               f"({u['cached_tokens']} cached), {u['completion_tokens']} out "
               f"+ {u['reasoning_tokens']} reasoning, ${u['cost_usd']:.4f}")
    typer.echo(f"wrote {out}/ (mapping.json, mapping_report.json, review_decisions.csv, "
               "clean/, exceptions.csv, summary.txt, usage.csv)")
    if truth:
        from onboard.score import score

        for k, v in score(rr.after, truth).as_dict().items():
            typer.echo(f"{k:<20} {v}")


_KIND = {"date": "dates", "labor": "labor", "dup": "duplicates"}


def _print_progress(stage: str, **info) -> None:
    """One or two lines per pipeline stage, so a long run shows where it is."""
    from collections import Counter

    echo = lambda m: typer.echo(m, err=True)  # noqa: E731
    if stage == "map":
        who = f"asking {info['model']}" if info["model"] else "using the given mapping"
        echo(f"> map: {info['sheets']} sheets, {info['rows']:,} rows, {who}")
    elif stage == "mapped":
        d = Counter((x["kind"], x["decision"]) for x in info["report"].get("decisions", []))
        if d:
            echo(f"  {d['sheet', 'accepted']} sheets, {d['column', 'accepted']} columns, "
                 f"{d['code', 'accepted']} code translations applied, "
                 f"{d['code', 'review']} left for a person")
    elif stage == "validate":
        echo("> validate: deterministic rules on every row")
    elif stage == "validated":
        s = info["result"].summary()
        echo(f"  {sum(s['cells_fixed'].values()):,} cells fixed by rule, "
             f"{sum(s['review_items'].values()):,} items need a decision")
    elif stage == "review":
        k = Counter(c.kind for c in info["candidates"])
        echo(f"> review: {sum(k.values())} closed questions to the LLM ("
             + ", ".join(f"{n} {_KIND.get(t, t)}" for t, n in k.most_common()) + ")")
    elif stage == "reviewed":
        acc = sum(x["accepted"] for x in info["decisions"])
        s = info["result"].summary()
        echo(f"  {acc} answers accepted (confidence >= 0.85), re-validated: "
             f"{sum(s['review_items'].values()):,} items still need a decision")


@app.command()
def version() -> None:
    """Print the package version."""
    from onboard import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
