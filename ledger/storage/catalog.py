"""DuckDB catalog interface, Hive-partitioned view manager, and zero-copy query engine.

Connects DuckDB directly to append-only Parquet storage without physical ingestion,
providing:
1. Canonical view registration for fact and dimension tables.
2. Derived bitemporal views (`v_bitemporal_ticker_map`, `v_bitemporal_fundamentals`).
3. Zero-copy Arrow memory transport returning native Polars DataFrames.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import duckdb
import polars as pl

from ledger.core.entity import register_ticker_map_view


class LedgerCatalog:
    """Manages DuckDB connections and canonical analytical views over raw Parquet files."""

    def __init__(
        self,
        base_dir: Path | str = "data/raw",
        db_path: str = ":memory:",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.db_path = db_path
        self.conn = duckdb.connect(database=db_path)
        self.refresh_views()

    def get_connection(self) -> duckdb.DuckDBPyConnection:
        """Return the active DuckDB connection."""
        return self.conn

    def refresh_views(self) -> None:
        """Register or refresh canonical views pointing to the partitioned Parquet files."""
        self._register_market_ohlcv_view()
        self._register_corporate_actions_view()
        self._register_entity_map_view()
        self._register_bitemporal_views()

    def _register_market_ohlcv_view(self) -> None:
        """Register `fact_market_ohlcv_raw` view from partitioned Parquet files."""
        ohlcv_dir = self.base_dir / "market_ohlcv"
        parquet_files = list(ohlcv_dir.glob("*/*/*.parquet"))

        if parquet_files:
            glob_path = str(ohlcv_dir / "year=*/month=*/*.parquet").replace("\\", "/")
            sql = f"""
            CREATE OR REPLACE VIEW fact_market_ohlcv_raw AS
            SELECT
                sec_id,
                trade_date,
                open,
                high,
                low,
                close,
                volume,
                known_from,
                ingestion_seq
            FROM read_parquet('{glob_path}', hive_partitioning=true);
            """
        else:
            # Placeholder empty schema when no partition files exist yet
            sql = """
            CREATE OR REPLACE VIEW fact_market_ohlcv_raw AS
            SELECT
                CAST(NULL AS VARCHAR) AS sec_id,
                CAST(NULL AS DATE) AS trade_date,
                CAST(NULL AS DOUBLE) AS open,
                CAST(NULL AS DOUBLE) AS high,
                CAST(NULL AS DOUBLE) AS low,
                CAST(NULL AS DOUBLE) AS close,
                CAST(NULL AS BIGINT) AS volume,
                CAST(NULL AS TIMESTAMP) AS known_from,
                CAST(NULL AS BIGINT) AS ingestion_seq
            WHERE 1 = 0;
            """
        self.conn.execute(sql)

    def _register_corporate_actions_view(self) -> None:
        """Register `fact_corporate_actions` view from partitioned Parquet files."""
        ca_dir = self.base_dir / "corporate_actions"
        parquet_files = list(ca_dir.glob("*/*.parquet"))

        if parquet_files:
            glob_path = str(ca_dir / "year=*/*.parquet").replace("\\", "/")
            sql = f"""
            CREATE OR REPLACE VIEW fact_corporate_actions AS
            SELECT
                sec_id,
                action_type,
                ex_date,
                split_ratio,
                cash_amount,
                announcement_date,
                known_from,
                ingestion_seq
            FROM read_parquet('{glob_path}', hive_partitioning=true);
            """
        else:
            sql = """
            CREATE OR REPLACE VIEW fact_corporate_actions AS
            SELECT
                CAST(NULL AS VARCHAR) AS sec_id,
                CAST(NULL AS VARCHAR) AS action_type,
                CAST(NULL AS DATE) AS ex_date,
                CAST(NULL AS DOUBLE) AS split_ratio,
                CAST(NULL AS DOUBLE) AS cash_amount,
                CAST(NULL AS DATE) AS announcement_date,
                CAST(NULL AS TIMESTAMP) AS known_from,
                CAST(NULL AS BIGINT) AS ingestion_seq
            WHERE 1 = 0;
            """
        self.conn.execute(sql)

    def _register_entity_map_view(self) -> None:
        """Register `dim_security_ticker_raw` view from Parquet files."""
        entity_dir = self.base_dir / "entity_map"
        parquet_files = list(entity_dir.glob("*.parquet"))

        if parquet_files:
            glob_path = str(entity_dir / "*.parquet").replace("\\", "/")
            sql = f"""
            CREATE OR REPLACE VIEW dim_security_ticker_raw AS
            SELECT
                sec_id,
                ticker,
                valid_from,
                ingestion_seq
            FROM read_parquet('{glob_path}');
            """
        else:
            sql = """
            CREATE OR REPLACE VIEW dim_security_ticker_raw AS
            SELECT
                CAST(NULL AS VARCHAR) AS sec_id,
                CAST(NULL AS VARCHAR) AS ticker,
                CAST(NULL AS TIMESTAMP) AS valid_from,
                CAST(NULL AS BIGINT) AS ingestion_seq
            WHERE 1 = 0;
            """
        self.conn.execute(sql)

    def _register_bitemporal_views(self) -> None:
        """Register derived bitemporal views."""
        register_ticker_map_view(
            conn=self.conn,
            table_name="dim_security_ticker_raw",
            view_name="v_bitemporal_ticker_map",
        )

    def query(self, sql: str, params: list[Any] | None = None) -> pl.DataFrame:
        """Execute a SQL query against the DuckDB catalog and return a Polars DataFrame."""
        if params:
            return self.conn.execute(sql, params).pl()
        return self.conn.execute(sql).pl()

    def close(self) -> None:
        """Close the catalog connection."""
        self.conn.close()
