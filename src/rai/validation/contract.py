"""Declarative data contracts (FR-03.1, FR-03.4): YAML schema, parser and rule expansion.

A contract describes one asset (for example ``transactions_raw``). It is parsed
into a typed, validated :class:`Contract`, then expanded into a flat list of
:class:`Rule` objects that the rule engine (B-09) evaluates. Each contract
version has a canonical JSON form and a SHA-256 (``spec_sha256``), which is what
runs pin and what makes a published version immutable (Data Model section 2).

Format (Data Model section 9)::

    contract: transactions_raw
    version: 1
    freshness: { column: created_at, max_lag: 1h }
    primary_key: [txn_id]
    unique_across_batches: [txn_id]
    columns:
      amount: { type: decimal(14,2), required: true, min: 0.01, max: 25000,
                severity: critical, threshold: "0.1%" }
    schema_change_policy: { added_column: non_breaking, ... }
    severity_default: critical
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Any, Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

Severity = Literal["info", "warn", "critical"]
ChangeImpact = Literal["breaking", "non_breaking"]
BaseType = Literal["string", "integer", "decimal", "float", "boolean", "date", "timestamp", "uuid"]

NAME = r"^[a-z][a-z0-9_]{0,62}$"
_DECIMAL_TYPE = re.compile(r"^decimal\((\d{1,2}),(\d{1,2})\)$")
_DURATION = re.compile(r"^(\d+)(s|m|h|d)$")
_DURATION_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}
_PERCENT = re.compile(r"^(\d+(?:\.\d+)?)%$")
ORDERED_TYPES = {"integer", "decimal", "float", "date", "timestamp"}
TEXT_TYPES = {"string", "uuid"}


class ContractError(ValueError):
    """A contract that cannot be used; ``problems`` lists every issue with its path."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("invalid contract:\n" + "\n".join(f"  - {p}" for p in problems))


@dataclass(frozen=True, slots=True)
class ColumnType:
    base: BaseType
    precision: int | None = None
    scale: int | None = None

    def __str__(self) -> str:
        return f"decimal({self.precision},{self.scale})" if self.base == "decimal" else self.base


def parse_type(text: str) -> ColumnType:
    value = text.strip().lower().replace(" ", "")
    if match := _DECIMAL_TYPE.match(value):
        precision, scale = int(match[1]), int(match[2])
        if not 1 <= precision <= 38 or scale > precision:
            raise ValueError(
                f"decimal({precision},{scale}) needs 1 <= precision <= 38, scale <= precision"
            )
        return ColumnType("decimal", precision, scale)
    if value == "decimal":
        return ColumnType("decimal", 38, 2)
    if re.match(r"^decimal\(\d+$", value):
        raise ValueError(
            f"type {text!r} looks cut at the comma: inside {{ ... }} write "
            'type: "decimal(p,s)" in quotes, or use one key per line'
        )
    if value in {"string", "integer", "float", "boolean", "date", "timestamp", "uuid"}:
        return ColumnType(value)  # type: ignore[arg-type]
    raise ValueError(
        f"unknown type {text!r}; use string, integer, decimal(p,s), float, boolean, date, "
        "timestamp or uuid"
    )


def parse_duration(text: str) -> timedelta:
    match = _DURATION.match(text.strip())
    if not match or int(match[1]) == 0:
        raise ValueError(f"duration {text!r} must look like 30s, 15m, 1h or 2d")
    return timedelta(**{_DURATION_UNITS[match[2]]: int(match[1])})


@dataclass(frozen=True, slots=True)
class Threshold:
    """How many failing rows a rule tolerates before it fails (FR-03.4)."""

    max_count: int | None = None
    max_fraction: Decimal | None = None

    def allows(self, failing: int, total: int) -> bool:
        if self.max_fraction is not None:
            return total == 0 or Decimal(failing) / Decimal(total) <= self.max_fraction
        return failing <= (self.max_count or 0)

    def __str__(self) -> str:
        if self.max_fraction is not None:
            return f"{(self.max_fraction * 100).normalize():f}%"
        return str(self.max_count or 0)


def parse_threshold(value: int | str) -> Threshold:
    if isinstance(value, bool):
        raise ValueError("threshold must be a row count or a percentage such as '0.5%'")
    if isinstance(value, int):
        if value < 0:
            raise ValueError("threshold count cannot be negative")
        return Threshold(max_count=value)
    match = _PERCENT.match(value.strip())
    if not match or Decimal(match[1]) > 100:
        raise ValueError(f"threshold {value!r} must be a row count or a percentage such as '0.5%'")
    return Threshold(max_fraction=Decimal(match[1]) / 100)


Scalar = str | int | float | bool
Ref = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")]


class MaxLookup(BaseModel):
    """Per-row upper bound read from a reference table, e.g. each account's limit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: Ref
    key: str


class ColumnSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    required: bool = False
    unique: bool = False
    pattern: str | None = None
    min: Scalar | None = None
    max: Scalar | None = None
    allowed: list[Scalar] | None = Field(default=None, min_length=1)
    references: Ref | None = None
    max_lookup: MaxLookup | None = None
    severity: Severity | None = None
    threshold: int | str | None = None
    description: str | None = None

    @field_validator("type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        return str(parse_type(value))

    @field_validator("pattern")
    @classmethod
    def _valid_regex(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as error:
                raise ValueError(f"pattern is not a valid regular expression: {error}") from error
        return value

    @field_validator("threshold")
    @classmethod
    def _valid_threshold(cls, value: int | str | None) -> int | str | None:
        if value is not None:
            parse_threshold(value)
        return value

    @model_validator(mode="after")
    def _options_fit_the_type(self) -> Self:
        column_type = parse_type(self.type)
        if (self.min is not None or self.max is not None) and column_type.base not in ORDERED_TYPES:
            raise ValueError(f"min/max need an ordered type, not {column_type}")
        if self.max_lookup is not None and column_type.base not in {"integer", "decimal", "float"}:
            raise ValueError(f"max_lookup needs a numeric column, not {column_type}")
        if self.pattern is not None and column_type.base not in TEXT_TYPES:
            raise ValueError(f"pattern needs a string or uuid column, not {column_type}")
        if column_type.base in {"integer", "decimal", "float"}:
            low, high = _number(self.min, "min"), _number(self.max, "max")
            if low is not None and high is not None and low > high:
                raise ValueError(f"min ({self.min}) is greater than max ({self.max})")
            for value in self.allowed or []:
                _number(value, "allowed value")
        if column_type.base in {"date", "timestamp"}:
            for bound in (self.min, self.max):
                if bound is not None and not isinstance(bound, str):
                    raise ValueError("date/timestamp min/max must be ISO 8601 strings")
        return self

    @property
    def column_type(self) -> ColumnType:
        return parse_type(self.type)


def _number(value: Scalar | None, label: str) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number, not a boolean")
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} {value!r} is not a number") from error


class Freshness(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    max_lag: str

    @field_validator("max_lag")
    @classmethod
    def _valid_duration(cls, value: str) -> str:
        parse_duration(value)
        return value


class SchemaChangePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    added_column: ChangeImpact = "non_breaking"
    removed_or_renamed_column: ChangeImpact = "breaking"
    type_change: ChangeImpact = "breaking"


ColumnName = Annotated[str, Field(pattern=NAME)]


class Contract(BaseModel):
    """A validated data contract. Construct it with :func:`load_contract`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract: str = Field(pattern=NAME)
    version: int = Field(ge=1)
    owner: str | None = None
    description: str | None = None
    freshness: Freshness | None = None
    primary_key: list[ColumnName] = Field(default_factory=list)
    unique_across_batches: list[ColumnName] = Field(default_factory=list)
    columns: dict[ColumnName, ColumnSpec] = Field(min_length=1)
    schema_change_policy: SchemaChangePolicy = Field(default_factory=SchemaChangePolicy)
    severity_default: Severity = "critical"
    threshold_default: int | str = 0

    @field_validator("threshold_default")
    @classmethod
    def _valid_threshold(cls, value: int | str) -> int | str:
        parse_threshold(value)
        return value

    @model_validator(mode="after")
    def _references_are_declared(self) -> Self:
        problems = []
        for label, names in (
            ("primary_key", self.primary_key),
            ("unique_across_batches", self.unique_across_batches),
        ):
            problems += [
                f"{label}: column {n!r} is not declared" for n in names if n not in self.columns
            ]
            if len(set(names)) != len(names):
                problems.append(f"{label}: lists a column twice")
        for name, column in self.columns.items():
            if column.max_lookup is not None and column.max_lookup.key not in self.columns:
                key = column.max_lookup.key
                problems.append(f"columns.{name}.max_lookup.key: column {key!r} is not declared")
        if self.freshness is not None:
            spec = self.columns.get(self.freshness.column)
            if spec is None:
                problems.append(
                    f"freshness.column: column {self.freshness.column!r} is not declared"
                )
            elif spec.column_type.base not in {"timestamp", "date"}:
                problems.append("freshness.column: must be a timestamp or date column")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def canonical_json(self) -> str:
        """Stable text of the contract; equal contracts give equal text."""
        data = self.model_dump(mode="json", exclude_none=True)
        return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @property
    def spec_sha256(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @property
    def ref(self) -> str:
        """Pin used by runs and reports, for example ``transactions_raw@3``."""
        return f"{self.contract}@{self.version}"

    def rules(self) -> list[Rule]:
        return expand_rules(self)


RuleKind = Literal[
    "required",
    "type",
    "pattern",
    "range",
    "max_lookup",
    "allowed",
    "references",
    "unique",
    "primary_key",
    "unique_across_batches",
    "freshness",
    "schema",
]


@dataclass(frozen=True, slots=True)
class Rule:
    """One check the engine runs. ``id`` is stable across runs and used in findings."""

    id: str
    kind: RuleKind
    columns: tuple[str, ...]
    severity: Severity
    threshold: Threshold
    params: dict[str, Any] = field(default_factory=dict)


def expand_rules(contract: Contract) -> list[Rule]:
    """Turn a contract into the flat, ordered rule list the engine evaluates."""
    default_threshold = parse_threshold(contract.threshold_default)
    rules = [
        Rule(
            "schema",
            "schema",
            tuple(contract.columns),
            contract.severity_default,
            Threshold(max_count=0),
            {"policy": contract.schema_change_policy.model_dump()},
        )
    ]
    single_pk = contract.primary_key if len(contract.primary_key) == 1 else []
    for name, spec in contract.columns.items():
        severity = spec.severity or contract.severity_default
        threshold = (
            parse_threshold(spec.threshold) if spec.threshold is not None else default_threshold
        )

        def add(
            kind: RuleKind,
            params: dict[str, Any] | None = None,
            *,
            column: str = name,
            severity: Severity = severity,
            threshold: Threshold = threshold,
        ) -> None:
            rules.append(
                Rule(f"{column}.{kind}", kind, (column,), severity, threshold, params or {})
            )

        if spec.required or name in contract.primary_key:
            add("required")
        add("type", {"type": spec.type})
        if spec.pattern is not None:
            add("pattern", {"pattern": spec.pattern})
        if spec.min is not None or spec.max is not None:
            add("range", {"min": spec.min, "max": spec.max})
        if spec.max_lookup is not None:
            table, column = spec.max_lookup.ref.split(".")
            add("max_lookup", {"table": table, "column": column, "key": spec.max_lookup.key})
        if spec.allowed is not None:
            add("allowed", {"values": list(spec.allowed)})
        if spec.references is not None:
            table, column = spec.references.split(".")
            add("references", {"table": table, "column": column})
        if spec.unique or name in single_pk:
            add("unique")
    if len(contract.primary_key) > 1:
        rules.append(
            Rule(
                "primary_key",
                "primary_key",
                tuple(contract.primary_key),
                contract.severity_default,
                default_threshold,
            )
        )
    if contract.unique_across_batches:
        cols = tuple(contract.unique_across_batches)
        rules.append(
            Rule(
                "unique_across_batches:" + "+".join(cols),
                "unique_across_batches",
                cols,
                contract.severity_default,
                default_threshold,
            )
        )
    if contract.freshness is not None:
        rules.append(
            Rule(
                "freshness",
                "freshness",
                (contract.freshness.column,),
                contract.columns[contract.freshness.column].severity or contract.severity_default,
                default_threshold,
                {
                    "max_lag_seconds": int(
                        parse_duration(contract.freshness.max_lag).total_seconds()
                    )
                },
            )
        )
    return rules


# ---- loading ---------------------------------------------------------------------


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of silently keeping the last."""


def _no_duplicates(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> Any:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=deep)


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _location(loc: tuple[int | str, ...]) -> str:
    return ".".join(str(part) for part in loc) or "contract"


def parse_contract(text: str) -> Contract:
    """Parse and validate contract YAML (or JSON). Raises :class:`ContractError`."""
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as error:
        raise ContractError([f"not valid YAML: {error}"]) from error
    if not isinstance(data, dict):
        raise ContractError(["the contract must be a YAML mapping"])
    try:
        return Contract.model_validate(data)
    except ValidationError as error:
        problems = []
        for issue in error.errors():
            message = issue["msg"].removeprefix("Value error, ")
            problems.append(f"{_location(issue['loc'])}: {message}")
        raise ContractError(problems) from error


def load_contract(path: Path) -> Contract:
    return parse_contract(path.read_text(encoding="utf-8"))


def contract_json_schema() -> dict[str, Any]:
    """JSON Schema of the contract format, for editors and API validation."""
    schema = Contract.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "RAI data contract"
    return schema
