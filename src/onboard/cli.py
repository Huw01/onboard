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
def version() -> None:
    """Print the package version."""
    from onboard import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
