"""Connector configuration, the common interface and text normalisation (FR-02.1).

Every connector returns a batch as **text columns** in the forms the rule engine
expects, whatever the source stores:

* timestamps with a time zone: ``2026-01-01T00:00:00.010000Z`` (UTC);
  without one: ``2026-01-01T00:00:00.010000`` (the engine then reports a type
  error, because the contract cannot know which zone was meant);
* dates ``YYYY-MM-DD``; booleans ``true``/``false``; decimals as exact text;
  missing values as null.

So a Parquet file or a PostgreSQL table validates exactly like the same data in a
CSV file. Each connector is bound to one environment and refuses to read for
another (environment scoping, FR-02.1).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Annotated, Any, Literal

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rai.connectors.secrets import SecretRef, scrub

_SECRET_KEYS = ("password", "passwd", "pwd", "secret", "token", "api_key", "apikey", "dsn")
NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"


class ConnectorError(RuntimeError):
    """A connector failure. Messages are scrubbed of secrets before they are raised."""

    def __init__(self, message: str, *secrets: Any) -> None:
        super().__init__(scrub(message, *secrets))


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    environment: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _no_literal_secrets(cls, data: Any) -> Any:
        if isinstance(data, dict):
            leaked = [
                k
                for k in data
                if k != "secret_ref" and any(word in k.lower() for word in _SECRET_KEYS)
            ]
            if leaked:
                raise ValueError(
                    f"{', '.join(sorted(leaked))}: credentials may not appear in a connector "
                    "config; use secret_ref: env:NAME or file:/run/secrets/name"
                )
        return data


class FileConfig(_Config):
    type: Literal["csv", "parquet"]
    root: str = Field(min_length=1)


class PostgresConfig(_Config):
    type: Literal["postgres"]
    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    secret_ref: str
    sslmode: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "prefer"
    schema_name: str = Field(default="public", pattern=NAME)
    statement_timeout_seconds: int = Field(default=60, ge=1, le=3600)

    @field_validator("secret_ref")
    @classmethod
    def _valid_ref(cls, value: str) -> str:
        SecretRef.parse(value)
        return value


ConnectorConfig = Annotated[FileConfig | PostgresConfig, Field(discriminator="type")]


class _Holder(BaseModel):
    config: ConnectorConfig


def parse_config(data: dict[str, Any]) -> FileConfig | PostgresConfig:
    try:
        return _Holder.model_validate({"config": data}).config
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in issue['loc'][2:]) or 'config'}: "
            f"{issue['msg'].removeprefix('Value error, ')}"
            for issue in error.errors()
        )
        raise ConnectorError(f"invalid connector config: {problems}") from None


class Connector(ABC):
    """Reads assets from one source within one environment."""

    def __init__(self, config: FileConfig | PostgresConfig) -> None:
        self.config = config

    def read(self, asset: str, *, environment: str, limit: int | None = None) -> pl.DataFrame:
        if environment != self.config.environment:
            raise ConnectorError(
                f"connector {self.config.name!r} belongs to environment "
                f"{self.config.environment!r}, not {environment!r}"
            )
        if limit is not None and limit < 1:
            raise ConnectorError("limit must be at least 1")
        return self._read(asset, limit)

    @abstractmethod
    def _read(self, asset: str, limit: int | None) -> pl.DataFrame: ...

    def describe(self) -> dict[str, Any]:
        """Safe to log or store: only references, never secret values."""
        return self.config.model_dump()


def to_text(frame: pl.DataFrame) -> pl.DataFrame:
    """Every column as text, in the forms the rule engine expects."""
    columns = []
    for name, dtype in frame.schema.items():
        column = pl.col(name)
        if isinstance(dtype, pl.Datetime):
            if dtype.time_zone:
                text = column.dt.convert_time_zone("UTC").dt.strftime("%Y-%m-%dT%H:%M:%S%.6fZ")
            else:
                text = column.dt.strftime("%Y-%m-%dT%H:%M:%S%.6f")
        elif dtype == pl.Date:
            text = column.dt.strftime("%Y-%m-%d")
        elif dtype == pl.Boolean:
            text = pl.when(column).then(pl.lit("true")).when(~column).then(pl.lit("false"))
        else:
            text = column.cast(pl.Utf8)
        columns.append(text.alias(name))
    return frame.select(columns) if columns else frame
