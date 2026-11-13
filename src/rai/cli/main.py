"""Entry point for the ``rai`` command.

Commands are added backlog item by backlog item; ``validate`` arrives with B-14.
"""

import typer
from rich.console import Console

from rai import __version__

app = typer.Typer(
    name="rai",
    help="Resilinet-AI: sandbox-only data-integrity and recovery assurance.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.callback()
def main() -> None:
    """Resilinet-AI command-line interface."""


@app.command()
def version() -> None:
    """Print the RAI version."""
    console.print(f"rai {__version__}")
