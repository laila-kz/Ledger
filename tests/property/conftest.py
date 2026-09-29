"""Hypothesis strategies for property-based testing of bitemporal derivation engine."""

from datetime import datetime, timedelta, timezone

import polars as pl
from hypothesis import strategies as st
from hypothesis.strategies import DrawFn

BASE_DATETIME = datetime(2020, 1, 1, tzinfo=timezone.utc)

IngestionEvent = dict[str, object]


@st.composite
def ingestion_event(draw: DrawFn) -> IngestionEvent:
    """Generate a single raw bitemporal ingestion event record."""
    sec_id = draw(st.sampled_from(["SEC_AAPL_001", "SEC_TSLA_001", "SEC_NVDA_001"]))
    metric_name = draw(st.sampled_from(["close", "eps", "revenue"]))

    # Generate valid_from (offset in days from base)
    valid_offset = draw(st.integers(min_value=0, max_value=365))
    valid_from = BASE_DATETIME + timedelta(days=valid_offset)

    # Generate known_from (offset relative to valid_from to test causality)
    known_offset_days = draw(st.integers(min_value=0, max_value=60))
    known_offset_hours = draw(st.integers(min_value=0, max_value=23))
    known_from = valid_from + timedelta(days=known_offset_days, hours=known_offset_hours)

    value = draw(st.floats(min_value=1.0, max_value=1000.0, allow_nan=False, allow_infinity=False))

    return {
        "sec_id": sec_id,
        "metric_name": metric_name,
        "valid_from": valid_from,
        "known_from": known_from,
        "value": value,
    }


@st.composite
def event_sequence(draw: DrawFn, min_size: int = 1, max_size: int = 15) -> list[IngestionEvent]:
    """Generate a sequence of ingestion events with assigned ingestion sequence numbers."""
    events: list[IngestionEvent] = draw(
        st.lists(ingestion_event(), min_size=min_size, max_size=max_size)
    )
    # Assign monotonic ingestion_seq
    for seq, event in enumerate(events):
        event["ingestion_seq"] = seq
    return events


@st.composite
def bitemporal_polars_dataframe(
    draw: DrawFn, min_size: int = 1, max_size: int = 15
) -> pl.DataFrame:
    """Generate a Polars DataFrame populated with raw append-only ingestion events."""
    events = draw(event_sequence(min_size=min_size, max_size=max_size))
    schema: dict[str, pl.DataType | type[pl.DataType]] = {
        "sec_id": pl.String,
        "metric_name": pl.String,
        "valid_from": pl.Datetime("us", "UTC"),
        "known_from": pl.Datetime("us", "UTC"),
        "ingestion_seq": pl.Int64,
        "value": pl.Float64,
    }
    return pl.DataFrame(events, schema=schema)
