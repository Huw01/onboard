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
def version() -> None:
    """Print the package version."""
    from onboard import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
