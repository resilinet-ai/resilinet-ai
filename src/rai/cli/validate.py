"""``rai validate``: validate one batch against a contract (B-14, FR-06.1).

Offline mode needs no server (Architecture section 5)::

    rai validate --contract contracts/sandboxbank/transactions_raw.yaml \\
        --data feed/transactions_raw/B000001.csv \\
        --ref accounts=feed/accounts.csv --ref merchants=feed/merchants.csv \\
        --history .rai/history.sqlite

Through a connector, the batch and reference tables are assets of that connector::

    rai validate --contract c.yaml --connector ledger.yaml --environment SANDBOX-01 \\
        --asset transactions_raw --ref accounts=accounts

Exit codes: 0 the batch passes, 1 it fails (a blocking rule failed or could not
run), 2 the command could not run (bad contract, missing file, connector error).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import polars as pl
import typer
import yaml
from rich.console import Console

from rai.connectors import ConnectorError, connector
from rai.connectors.base import to_text
from rai.validation.contract import ContractError, Severity, load_contract
from rai.validation.engine import ValidationReport, read_batch, validate
from rai.validation.history import SqliteKeyStore
from rai.validation.report import file_sha256, report_dict, report_json
from rai.validation.verdict import Verdict, decide

console = Console(soft_wrap=True)
errors = Console(stderr=True, soft_wrap=True)


class UsageProblem(Exception):
    """Inputs that stop validation from running at all (exit code 2)."""


def _read_file(path: Path) -> pl.DataFrame:
    if not path.is_file():
        raise UsageProblem(f"data file {path} not found")
    suffix = path.suffix.lower()
    try:
        if suffix == ".csv":
            return read_batch(path)
        if suffix == ".parquet":
            return to_text(pl.read_parquet(path))
    except (pl.exceptions.PolarsError, OSError) as error:
        raise UsageProblem(f"could not read {path}: {error}") from None
    raise UsageProblem(f"{path}: only .csv and .parquet files are supported")


def _parse_refs(refs: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in refs:
        name, sep, value = item.partition("=")
        if not sep or not name or not value:
            raise UsageProblem(f"--ref {item!r} must look like name=location")
        parsed[name] = value
    return parsed


def _parse_as_of(text: str | None) -> datetime | None:
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise UsageProblem(f"--as-of {text!r} is not an ISO 8601 timestamp") from None
    if moment.tzinfo is None:
        raise UsageProblem("--as-of needs a time zone, for example 2026-01-01T00:15:00Z")
    return moment


def _load_inputs(
    data: Path | None,
    connector_file: Path | None,
    asset: str | None,
    environment: str | None,
    refs: dict[str, str],
) -> tuple[pl.DataFrame, dict[str, pl.DataFrame], dict[str, Any], str]:
    if (data is None) == (connector_file is None):
        raise UsageProblem("give exactly one of --data FILE or --connector CONFIG")
    if data is not None:
        references = {name: _read_file(Path(location)) for name, location in refs.items()}
        frame = _read_file(data)
        source = {"type": "file", "path": data.as_posix(), "sha256": file_sha256(data)}
        return frame, references, source, data.stem
    if connector_file is None or not asset or not environment:
        raise UsageProblem("--connector needs --asset and --environment")
    try:
        config = yaml.safe_load(connector_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise UsageProblem(f"could not read connector config {connector_file}: {error}") from None
    if not isinstance(config, dict):
        raise UsageProblem(f"connector config {connector_file} must be a YAML mapping")
    try:
        source_connector = connector(config)
        frame = source_connector.read(asset, environment=environment)
        references = {
            name: source_connector.read(location, environment=environment)
            for name, location in refs.items()
        }
    except ConnectorError as error:
        raise UsageProblem(str(error)) from None
    source = {
        "type": "connector",
        "connector": source_connector.config.name,
        "environment": environment,
        "asset": asset,
    }
    return frame, references, source, asset


def _print_text(report: ValidationReport, verdict: Verdict, show_passing: bool) -> None:
    """One line per rule, plain ASCII: readable in a terminal of any width and in CI
    logs, never truncated, and easy to grep."""
    style = "bold green" if verdict.passed else "bold red"
    console.print(f"[{style}]{verdict.summary()}[/]", highlight=False)
    console.print(
        f"contract {report.contract_ref} (sha256 {report.contract_sha256[:12]}), "
        f"{report.row_count:,} rows, {len(report.results)} rules, "
        f"{report.duration_seconds:.2f} s",
        highlight=False,
    )
    marks = {"pass": ("ok  ", "green"), "fail": ("FAIL", "red"), "error": ("ERR ", "yellow")}
    for r in report.results:
        if r.outcome == "pass" and not show_passing:
            continue
        mark, colour = marks.get(r.outcome, (r.outcome.upper()[:4], ""))
        counted = f" {r.failing_count:,}/{r.row_count:,} rows" if r.outcome != "error" else ""
        line = f"  [{colour}]{mark}[/] {r.rule_id} ({r.severity}, threshold {r.threshold}){counted}"
        console.print(line, highlight=False)
        repeats_count = r.message == f"{r.failing_count} of {r.row_count} rows fail"
        if r.message and r.outcome != "pass" and not repeats_count:
            console.print(f"       {r.message}", highlight=False, markup=False)
        if r.sample_rows and r.outcome == "fail":
            rows = ", ".join(str(n) for n in r.sample_rows[:10])
            more = " ..." if r.failing_count > 10 else ""
            console.print(f"       first rows: {rows}{more}", highlight=False)
    for note in report.notes:
        console.print(f"note: {note}", highlight=False, markup=False)


def validate_command(
    contract: Annotated[Path, typer.Option(help="Contract YAML file")],
    data: Annotated[Path | None, typer.Option(help="Batch file (.csv or .parquet)")] = None,
    connector_file: Annotated[
        Path | None, typer.Option("--connector", help="Connector config YAML")
    ] = None,
    asset: Annotated[str | None, typer.Option(help="Asset to read through the connector")] = None,
    environment: Annotated[str | None, typer.Option(help="Environment of the connector")] = None,
    ref: Annotated[
        list[str] | None, typer.Option(help="Reference table, name=location (repeatable)")
    ] = None,
    as_of: Annotated[
        str | None, typer.Option(help="Batch arrival time (ISO 8601 with zone)")
    ] = None,
    batch_id: Annotated[
        str | None, typer.Option(help="Batch identity (default: file stem)")
    ] = None,
    history: Annotated[
        Path | None, typer.Option(help="Key history file for cross-batch duplicates")
    ] = None,
    record: Annotated[bool, typer.Option(help="Add this batch's keys to the history")] = True,
    fail_on: Annotated[str, typer.Option(help="Lowest blocking severity")] = "critical",
    output_format: Annotated[str, typer.Option("--format", help="text or json")] = "text",
    report_file: Annotated[
        Path | None, typer.Option("--report", help="Also write the JSON report to this file")
    ] = None,
    show_passing: Annotated[bool, typer.Option(help="List passing rules too")] = False,
) -> None:
    """Validate one batch against a contract; exit 0 pass, 1 fail, 2 could not run."""
    try:
        if fail_on not in ("info", "warn", "critical"):
            raise UsageProblem("--fail-on must be info, warn or critical")
        if output_format not in ("text", "json"):
            raise UsageProblem("--format must be text or json")
        try:
            parsed_contract = load_contract(contract)
        except ContractError as error:
            raise UsageProblem(f"{contract}: {error}") from None
        except OSError as error:
            raise UsageProblem(f"could not read contract {contract}: {error}") from None
        moment = _parse_as_of(as_of)
        frame, references, source, default_id = _load_inputs(
            data, connector_file, asset, environment, _parse_refs(ref or [])
        )
        identity = batch_id or default_id
        store = SqliteKeyStore(history) if history is not None else None
        try:
            report = validate(
                parsed_contract,
                frame,
                references=references,
                as_of=moment,
                history=store,
                batch_id=identity,
                record=record and store is not None,
            )
        finally:
            if store is not None:
                store.close()
    except UsageProblem as problem:
        errors.print(f"error: {problem}")
        raise typer.Exit(2) from None

    severity: Severity = fail_on  # type: ignore[assignment]  # checked above
    verdict = decide(report, fail_on=severity, subject=f"{report.contract_ref} {identity}")
    document = report_json(report_dict(report, verdict, source=source, batch_id=identity))
    if report_file is not None:
        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text(document, encoding="utf-8", newline="\n")
    if output_format == "json":
        typer.echo(document, nl=False)
    else:
        _print_text(report, verdict, show_passing)
    raise typer.Exit(verdict.exit_code)
