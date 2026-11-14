"""M02 data connectors: PostgreSQL, Parquet and CSV (Redshift later), FR-02.1 and FR-02.3.

Build one from its config with :func:`connector`; every connector returns batches
as text columns that the rule engine validates the same way whatever the source.
"""

from __future__ import annotations

from typing import Any

from rai.connectors.base import Connector, ConnectorError, parse_config


def connector(config: dict[str, Any]) -> Connector:
    """The connector for a config mapping (validated; literal credentials refused)."""
    parsed = parse_config(config)
    if parsed.type == "postgres":
        from rai.connectors.postgres import PostgresConnector

        return PostgresConnector(parsed)
    from rai.connectors.files import FileConnector

    return FileConnector(parsed)


__all__ = ["Connector", "ConnectorError", "connector", "parse_config"]
