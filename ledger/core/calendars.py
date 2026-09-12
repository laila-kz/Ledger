"""Exchange calendar service and actionable timestamp resolver for NYSE (XNYS).

Financial events (filings, corporate actions, EOD price bars) cannot be acted upon
at the instant of raw event time if the exchange is closed. This module encapsulates:
1. NYSE trading session boundaries, holidays, and early closes via exchange_calendars.
2. Conversion of raw event timestamps into realistic 'actionable' transaction timestamps.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime, timedelta

import exchange_calendars as xcals
import pandas as pd

NY_TZ = zoneinfo.ZoneInfo("America/New_York")
UTC_TZ = zoneinfo.ZoneInfo("UTC")


class NYSECalendarService:
    """Service wrapper for the New York Stock Exchange (XNYS) calendar.

    Handles market session boundaries, early closes, holidays, and actionable
    time adjustments for backtest point-in-time correctness.
    """

    def __init__(self, exchange: str = "XNYS") -> None:
        self.exchange = exchange
        self._calendar = xcals.get_calendar(exchange)

    @property
    def calendar(self) -> xcals.ExchangeCalendar:
        """Underlying exchange_calendars instance."""
        return self._calendar

    def _to_ny_datetime(self, dt: datetime) -> datetime:
        """Normalize a datetime to America/New_York timezone."""
        if dt.tzinfo is None:
            # If naive, assume UTC and convert to NY
            dt = dt.replace(tzinfo=UTC_TZ)
        return dt.astimezone(NY_TZ)

    def is_session(self, dt: date | datetime | str) -> bool:
        """Check if a given date is a valid trading session."""
        if isinstance(dt, datetime):
            dt = self._to_ny_datetime(dt).date()
        date_str = dt.isoformat() if isinstance(dt, date) else str(dt)
        return bool(self._calendar.is_session(date_str))

    def next_session(self, dt: date | datetime | str) -> date:
        """Get the next active trading session strictly after the provided date."""
        if isinstance(dt, datetime):
            dt = self._to_ny_datetime(dt).date()
        date_str = dt.isoformat() if isinstance(dt, date) else str(dt)
        ts = pd.Timestamp(date_str)
        next_ts = self._calendar.next_session(ts)
        return next_ts.date()  # type: ignore[no-any-return]

    def previous_session(self, dt: date | datetime | str) -> date:
        """Get the previous active trading session strictly before the provided date."""
        if isinstance(dt, datetime):
            dt = self._to_ny_datetime(dt).date()
        date_str = dt.isoformat() if isinstance(dt, date) else str(dt)
        ts = pd.Timestamp(date_str)
        prev_ts = self._calendar.previous_session(ts)
        return prev_ts.date()  # type: ignore[no-any-return]

    def session_open(self, session_date: date | str) -> datetime:
        """Get the market open timestamp (America/New_York) for a given session."""
        date_str = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
        ts = pd.Timestamp(date_str)
        open_ts = self._calendar.session_open(ts)
        # open_ts is UTC in exchange_calendars, convert to NY
        return open_ts.to_pydatetime().astimezone(NY_TZ)

    def session_close(self, session_date: date | str) -> datetime:
        """Get the market close timestamp (America/New_York) for a given session."""
        date_str = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
        ts = pd.Timestamp(date_str)
        close_ts = self._calendar.session_close(ts)
        return close_ts.to_pydatetime().astimezone(NY_TZ)

    def get_trading_sessions(self, start_date: date | str, end_date: date | str) -> list[date]:
        """Return all trading session dates between start_date and end_date (inclusive)."""
        start_str = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
        end_str = end_date.isoformat() if isinstance(end_date, date) else str(end_date)
        sessions = self._calendar.sessions_in_range(start_str, end_str)
        return [ts.date() for ts in sessions]

    def get_actionable_timestamp(
        self,
        event_time: datetime,
        is_market_data: bool = False,
        market_data_buffer_minutes: int = 15,
    ) -> datetime:
        """Convert a raw event timestamp into its actionable trading timestamp.

        Rules:
        1. Market Data (EOD OHLCV bars):
           - A daily bar for session D closes at e.g. 16:00 EST (or 13:00 on early close).
           - Data becomes actionable after post-market consolidation buffer
             (default 15 minutes: 16:15 EST / 13:15 EST).
        2. Non-Market Data (Filings, corporate actions, fundamental reports):
           - If published during an active trading session (between 09:30 and 16:00 EST):
             Actionable immediately at event_time.
           - If published after market close (>= 16:00 EST), before market open (< 09:30 EST),
             or on a weekend/holiday:
             Actionable at the NEXT trading session market open (09:30 EST).

        Args:
            event_time: The raw event timestamp.
            is_market_data: True if this is an EOD price bar / trade record.
            market_data_buffer_minutes: Minutes buffer post-close for market data consolidation.

        Returns:
            The actionable timestamp in America/New_York timezone.
        """
        ny_time = self._to_ny_datetime(event_time)
        event_date = ny_time.date()

        if is_market_data:
            # For market data, find the session close
            if self.is_session(event_date):
                close_dt = self.session_close(event_date)
            else:
                # If timestamp fell on non-session, find previous session close
                prev_date = self.previous_session(event_date)
                close_dt = self.session_close(prev_date)

            return close_dt + timedelta(minutes=market_data_buffer_minutes)

        # For non-market data (filings, restatements, news):
        if self.is_session(event_date):
            open_dt = self.session_open(event_date)
            close_dt = self.session_close(event_date)

            if ny_time < open_dt:
                # Arrived before market open -> actionable at today's open (09:30)
                return open_dt
            if ny_time < close_dt:
                # Arrived during trading session -> actionable immediately
                return ny_time

            # Arrived after market close -> actionable next session open (09:30)
            next_date = self.next_session(event_date)
            return self.session_open(next_date)

        # Arrived on weekend or holiday -> actionable at next session open
        next_date = self.next_session(event_date)
        return self.session_open(next_date)


# Singleton instance for default NYSE calendar
default_nyse_calendar = NYSECalendarService()


def get_actionable_timestamp(
    event_time: datetime,
    is_market_data: bool = False,
    market_data_buffer_minutes: int = 15,
) -> datetime:
    """Convenience functional wrapper using the default NYSE calendar."""
    return default_nyse_calendar.get_actionable_timestamp(
        event_time=event_time,
        is_market_data=is_market_data,
        market_data_buffer_minutes=market_data_buffer_minutes,
    )
