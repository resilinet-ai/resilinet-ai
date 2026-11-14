"""Value-level type checks shared by the rule engine and schema drift detection.

Every function returns a Polars expression over one text column, so callers can
combine them into a single vectorised pass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import polars as pl

from rai.validation.contract import ColumnType

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
_INTEGER = r"^-?[0-9]{1,18}$"
_TIMESTAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$"


# ---- expressions ---------------------------------------------------------------------


def missing(name: str) -> pl.Expr:
    column = pl.col(name)
    return column.is_null() | (column.str.strip_chars() == "")


def decimal_pattern(column_type: ColumnType) -> str:
    precision, scale = column_type.precision or 38, column_type.scale or 0
    whole = max(precision - scale, 1)
    fraction = rf"(\.[0-9]{{1,{scale}}})?" if scale else ""
    return rf"^-?[0-9]{{1,{whole}}}{fraction}$"


def valid(name: str, column_type: ColumnType) -> pl.Expr:
    """True where a present value fits the declared type."""
    text = pl.col(name).str.strip_chars()
    match column_type.base:
        case "string":
            return pl.lit(value=True)
        case "uuid":
            return text.str.contains(_UUID)
        case "integer":
            return text.str.contains(_INTEGER)
        case "decimal":
            return text.str.contains(decimal_pattern(column_type))
        case "float":
            return text.cast(pl.Float64, strict=False).is_not_null()
        case "boolean":
            return text.str.to_lowercase().is_in(["true", "false"])
        case "date":
            return text.str.to_date("%Y-%m-%d", strict=False).is_not_null()
        case "timestamp":
            return text.str.contains(_TIMESTAMP) & as_timestamp(name).is_not_null()
    raise AssertionError(column_type)  # pragma: no cover - ColumnType is a closed set


def as_timestamp(name: str) -> pl.Expr:
    iso = pl.col(name).str.strip_chars().str.replace(r"Z$", "+00:00")
    return iso.str.to_datetime(
        format="%Y-%m-%dT%H:%M:%S%.f%:z", strict=False, time_unit="us", time_zone="UTC"
    )


def typed(name: str, column_type: ColumnType) -> pl.Expr:
    """The value in its declared type, or null where missing or malformed."""
    present = ~missing(name) & valid(name, column_type)
    text = pl.col(name).str.strip_chars()
    match column_type.base:
        case "integer" | "decimal" | "float":
            value = text.cast(pl.Float64, strict=False)
        case "date":
            value = text.str.to_date("%Y-%m-%d", strict=False)
        case "timestamp":
            value = as_timestamp(name)
        case _:
            value = text
    return pl.when(present).then(value)


def bound(value: Any, column_type: ColumnType) -> Any:
    if column_type.base in {"integer", "decimal", "float"}:
        return float(Decimal(str(value)))
    if column_type.base == "date":
        return datetime.fromisoformat(str(value)).date()
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
