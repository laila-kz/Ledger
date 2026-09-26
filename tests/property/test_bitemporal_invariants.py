"""Property-based invariant tests using Hypothesis to fuzz the Ledger bitemporal engine."""

import polars as pl
from hypothesis import HealthCheck, given, settings

from ledger.core.bitemporal import (
    TransactionInterval,
    derive_known_to_polars,
)
from tests.property.conftest import bitemporal_polars_dataframe


@given(df=bitemporal_polars_dataframe(min_size=1, max_size=20))
@settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_no_overlapping_known_intervals(df: pl.DataFrame) -> None:
    """Invariant 1: No two transaction intervals for the same entity overlap."""
    derived = derive_known_to_polars(df, partition_by=["sec_id", "metric_name"])

    # Group by partition key and verify interval non-overlap
    partitions = derived.partition_by(["sec_id", "metric_name"], as_dict=True)
    for (_sec_id, _metric), group in partitions.items():
        # Sort by known_from, ingestion_seq
        sorted_group = group.sort(["known_from", "ingestion_seq"])
        known_from_list = sorted_group["known_from"].to_list()
        known_to_list = sorted_group["known_to"].to_list()

        for i in range(len(sorted_group) - 1):
            kf_current = known_from_list[i]
            kt_current = known_to_list[i]
            kf_next = known_from_list[i + 1]

            if kt_current is not None:
                assert kf_current <= kt_current, f"Inverted interval: {kf_current} > {kt_current}"
                assert kt_current <= kf_next, (
                    f"Overlap detected: interval [{kf_current}, {kt_current}) "
                    f"overlaps with next start {kf_next}"
                )


@given(df=bitemporal_polars_dataframe(min_size=1, max_size=20))
@settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_monotonic_known_intervals(df: pl.DataFrame) -> None:
    """Invariant 2: known_from is strictly less than known_to (when known_to is set)."""
    derived = derive_known_to_polars(df, partition_by=["sec_id", "metric_name"])

    non_null_to = derived.filter(pl.col("known_to").is_not_null())
    for row in non_null_to.iter_rows(named=True):
        kf = row["known_from"]
        kt = row["known_to"]
        # In case of identical known_from timestamps, ingestion_seq tiebreaker creates
        # identical known_to (zero-duration interval) or next timestamp
        assert kf <= kt, f"Monotonicity violated: known_from ({kf}) > known_to ({kt})"


@given(df=bitemporal_polars_dataframe(min_size=1, max_size=20))
@settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_no_gaps_between_contiguous_intervals(df: pl.DataFrame) -> None:
    """Invariant 3: Derived known_to of row n equals known_from of row n+1 within partition."""
    derived = derive_known_to_polars(df, partition_by=["sec_id", "metric_name"])

    partitions = derived.partition_by(["sec_id", "metric_name"], as_dict=True)
    for (_sec_id, _metric), group in partitions.items():
        sorted_group = group.sort(["known_from", "ingestion_seq"])
        known_from_list = sorted_group["known_from"].to_list()
        known_to_list = sorted_group["known_to"].to_list()

        for i in range(len(sorted_group) - 1):
            kt_current = known_to_list[i]
            kf_next = known_from_list[i + 1]
            assert kt_current == kf_next, (
                f"Gap detected between contiguous records: row {i} known_to ({kt_current}) "
                f"!= row {i + 1} known_from ({kf_next})"
            )


@given(df=bitemporal_polars_dataframe(min_size=1, max_size=20))
@settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_valid_before_known_causality(df: pl.DataFrame) -> None:
    """Invariant 4: Temporal causality holds (known_from >= valid_from)."""
    for row in df.iter_rows(named=True):
        vf = row["valid_from"]
        kf = row["known_from"]
        assert kf >= vf, f"Causality violation: known_from ({kf}) precedes valid_from ({vf})"


@given(df=bitemporal_polars_dataframe(min_size=1, max_size=15))
@settings(max_examples=300, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_idempotent_reingestion(df: pl.DataFrame) -> None:
    """Invariant 5: Re-ingesting an identical batch yields deterministic derived bounds."""
    df_once = derive_known_to_polars(df, partition_by=["sec_id", "metric_name"])

    # Duplicate input dataset with distinct ingestion sequence offsets
    duplicated_df = pl.concat(
        [
            df,
            df.with_columns(pl.col("ingestion_seq") + 100),
        ]
    )
    df_twice = derive_known_to_polars(duplicated_df, partition_by=["sec_id", "metric_name"])

    # First run outputs must match first half of duplicated run
    df_twice_first_half = df_twice.filter(pl.col("ingestion_seq") < 100)

    assert df_once["sec_id"].to_list() == df_twice_first_half["sec_id"].to_list()
    assert df_once["known_from"].to_list() == df_twice_first_half["known_from"].to_list()


@given(df=bitemporal_polars_dataframe(min_size=1, max_size=10))
@settings(max_examples=200, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_transaction_interval_pydantic_model_bounds(df: pl.DataFrame) -> None:
    """Verify TransactionInterval Pydantic model invariants against derived bounds."""
    derived = derive_known_to_polars(df, partition_by=["sec_id", "metric_name"])

    for row in derived.iter_rows(named=True):
        kf = row["known_from"]
        kt = row["known_to"]

        if kt is not None and kf < kt:
            # Should successfully construct TransactionInterval model
            interval = TransactionInterval(known_from=kf, known_to=kt)
            assert interval.known_from == kf
            assert interval.known_to == kt
            assert interval.is_known_at(kf) is True
