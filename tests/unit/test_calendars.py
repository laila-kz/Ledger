"""Unit tests for NYSE exchange calendar wrapper and actionable timestamp resolver."""

import zoneinfo
from datetime import date, datetime

from ledger.core.calendars import NYSECalendarService, get_actionable_timestamp

NY_TZ = zoneinfo.ZoneInfo("America/New_York")
UTC = zoneinfo.ZoneInfo("UTC")


class TestNYSECalendarService:
    """Tests for NYSE session queries, holidays, and market hours."""

    def test_session_detection(self) -> None:
        c = NYSECalendarService()
        # Wednesday June 14, 2023 was a normal trading day
        assert c.is_session(date(2023, 6, 14))
        # Saturday June 17, 2023 is not a trading day
        assert not c.is_session(date(2023, 6, 17))
        # Juneteenth (June 19, 2023) is a NYSE holiday
        assert not c.is_session(date(2023, 6, 19))

    def test_next_and_previous_session(self) -> None:
        c = NYSECalendarService()
        # Friday June 16, 2023 -> Next session is Tuesday June 20, 2023 (Juneteenth on Monday)
        next_s = c.next_session(date(2023, 6, 16))
        assert next_s == date(2023, 6, 20)

        # Tuesday June 20, 2023 -> Previous trading day is Friday June 16, 2023
        prev_s = c.previous_session(date(2023, 6, 20))
        assert prev_s == date(2023, 6, 16)

    def test_market_hours_bounds(self) -> None:
        c = NYSECalendarService()
        open_time = c.session_open(date(2023, 6, 14))
        close_time = c.session_close(date(2023, 6, 14))

        # Standard NYSE session: 09:30 - 16:00 EST
        assert open_time.hour == 9
        assert open_time.minute == 30
        assert close_time.hour == 16
        assert close_time.minute == 0


class TestActionableTimestampResolution:
    """Tests for get_actionable_timestamp under various market conditions."""

    def test_market_data_regular_session_buffer(self) -> None:
        # Wednesday June 14, 2023 at 16:00 EST
        event_time = datetime(2023, 6, 14, 16, 0, tzinfo=NY_TZ)
        actionable = get_actionable_timestamp(event_time, is_market_data=True)

        # Actionable 15 minutes post-close at 16:15 EST
        assert actionable == datetime(2023, 6, 14, 16, 15, tzinfo=NY_TZ)

    def test_market_data_early_close_buffer(self) -> None:
        # Black Friday (Friday Nov 24, 2023) has an early close at 13:00 EST
        event_time = datetime(2023, 11, 24, 13, 0, tzinfo=NY_TZ)
        actionable = get_actionable_timestamp(event_time, is_market_data=True)

        # Actionable 15 minutes post early-close at 13:15 EST
        assert actionable == datetime(2023, 11, 24, 13, 15, tzinfo=NY_TZ)

    def test_filing_during_market_hours(self) -> None:
        # 10-Q released on Wednesday June 14, 2023 at 10:30 EST
        event_time = datetime(2023, 6, 14, 10, 30, tzinfo=NY_TZ)
        actionable = get_actionable_timestamp(event_time, is_market_data=False)

        # Actionable immediately during session
        assert actionable == datetime(2023, 6, 14, 10, 30, tzinfo=NY_TZ)

    def test_filing_after_hours_friday(self) -> None:
        # 10-Q released on Friday June 16, 2023 at 17:00 EST (after market close)
        # Note: Mon June 19 is Juneteenth holiday, so next session is Tue June 20
        event_time = datetime(2023, 6, 16, 17, 0, tzinfo=NY_TZ)
        actionable = get_actionable_timestamp(event_time, is_market_data=False)

        # Actionable at next market open: Tuesday June 20 at 09:30 EST
        assert actionable == datetime(2023, 6, 20, 9, 30, tzinfo=NY_TZ)

    def test_filing_on_weekend(self) -> None:
        # Release on Saturday June 17, 2023 at 12:00 EST
        event_time = datetime(2023, 6, 17, 12, 0, tzinfo=NY_TZ)
        actionable = get_actionable_timestamp(event_time, is_market_data=False)

        # Actionable at next market open: Tuesday June 20 at 09:30 EST
        assert actionable == datetime(2023, 6, 20, 9, 30, tzinfo=NY_TZ)

    def test_filing_pre_market(self) -> None:
        # 8-K released Wednesday June 14, 2023 at 07:00 EST (before 09:30 open)
        event_time = datetime(2023, 6, 14, 7, 0, tzinfo=NY_TZ)
        actionable = get_actionable_timestamp(event_time, is_market_data=False)

        # Actionable at today's market open: 09:30 EST
        assert actionable == datetime(2023, 6, 14, 9, 30, tzinfo=NY_TZ)
