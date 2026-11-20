"""``rai canary scan``: search logs and evidence bundles for canary values (FR-05.1).

Offline: no RAI API or database. The key is SandboxBank's ``needles.csv`` or
``canaries.json``; the sources are files, folders, ZIP files (evidence bundles), or
``-`` for standard input::

    rai canary scan --key data/canaries/needles.csv serve.log worker.log bundles/
    docker compose logs --no-color | rai canary scan --key data/canaries/needles.csv -

Every hit names its source and place (``line 12``, or a JSON path in a bundle member)
with the value masked; no value is printed in full.

Exit codes: 0 no canary value; 1 a canary value was found; 2 the key cannot be used or
a source could not be read (fail closed).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from rai.privacy.sweep import load_key, sweep

canary_app = typer.Typer(
    help="Canary privacy sentinel: search logs and evidence bundles (FR-05.1).",
    no_args_is_help=True,
)


@canary_app.command("scan")
def scan_command(
    sources: Annotated[
        list[str],
        typer.Argument(help="Files, folders, ZIP files (evidence bundles), or - for stdin."),
    ],
    key: Annotated[
        Path, typer.Option("--key", help="The answer key: needles.csv or canaries.json.")
    ],
    case_sensitive: Annotated[
        bool, typer.Option(help="Match case exactly (by default ASCII case is ignored).")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print the result as JSON.")] = False,
) -> None:
    """Search SOURCES for every canary value of KEY; exit 1 on any hit."""
    found, problem = load_key(key)
    if problem:
        typer.echo(f"ERROR {problem}", err=True)
        raise typer.Exit(2)
    result = sweep(sources, found, case_sensitive=case_sensitive, key_path=key, stdin=sys.stdin)
    if as_json:
        typer.echo(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        for line in result.unreadable:
            typer.echo(f"ERROR   {line}")
        for line in result.skipped:
            typer.echo(f"SKIP    {line}")
        for f in result.sample:
            typer.echo(f"FAIL    {f.source} {f.place}: {f.kind} of {f.canary} ({f.masked})")
        if result.hits > len(result.sample):
            typer.echo(f"        ... and {result.hits - len(result.sample)} more")
        verdict = {"pass": "CLEAN", "fail": "FOUND", "error": "ERROR"}[result.outcome]
        typer.echo(f"{verdict:<7} {result.summary()}")
    raise typer.Exit({"pass": 0, "fail": 1, "error": 2}[result.outcome])
