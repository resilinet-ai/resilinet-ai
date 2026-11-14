"""CSV and Parquet connectors scoped to one root folder (FR-02.1).

An asset is a path relative to the connector's ``root``. Absolute paths and any
path that resolves outside the root (``../``, symlinks) are refused, so a
connector registered for one environment's folder cannot read another's.
"""

from __future__ import annotations

from pathlib import Path

import polars as pl

from rai.connectors.base import Connector, ConnectorError, FileConfig, to_text


class FileConnector(Connector):
    config: FileConfig

    def _path(self, asset: str) -> Path:
        root = Path(self.config.root).resolve()
        if Path(asset).is_absolute() or Path(asset).drive:
            raise ConnectorError(f"asset {asset!r} must be a path relative to the connector root")
        path = (root / asset).resolve()
        if not path.is_relative_to(root):
            raise ConnectorError(f"asset {asset!r} is outside the connector root")
        if not path.is_file():
            raise ConnectorError(f"asset {asset!r} not found under the connector root")
        return path

    def _read(self, asset: str, limit: int | None) -> pl.DataFrame:
        path = self._path(asset)
        try:
            if self.config.type == "csv":
                return pl.read_csv(path, infer_schema=False, n_rows=limit)
            frame = pl.read_parquet(path, n_rows=limit)
        except (pl.exceptions.PolarsError, OSError) as error:
            raise ConnectorError(f"could not read {asset!r}: {error}") from None
        return to_text(frame)
