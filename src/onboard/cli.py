"""Command-line interface."""

import typer

app = typer.Typer(help="LLM-assisted data onboarding for enterprise systems.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the package version."""
    from onboard import __version__

    typer.echo(__version__)


if __name__ == "__main__":
    app()
