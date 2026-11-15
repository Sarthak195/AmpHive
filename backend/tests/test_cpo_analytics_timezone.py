"""C12: CPO dashboard 'today' and daily series use the tenant's local day,
not the UTC day (DB-free: pure helpers + query compilation). A fixed +05:30
offset stands in for Asia/Kolkata so the tests do not need the IANA database
(absent on some dev boxes)."""
from datetime import datetime, timedelta, timezone, tzinfo

from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from backend.routers.cpo._analytics import _local_day, _local_midnight_utc

IST = timezone(timedelta(hours=5, minutes=30))


class _Zone(tzinfo):
    """Fixed-offset tzinfo that also carries an IANA-style `key`, like ZoneInfo."""

    key = "Asia/Kolkata"


def test_today_start_is_local_midnight_not_utc_midnight():
    # 03:00 IST on 16 Nov == 21:30 UTC on 15 Nov: still the IST 16th.
    now = datetime(2026, 11, 15, 21, 30, tzinfo=timezone.utc)
    assert _local_midnight_utc(IST, now) == datetime(2026, 11, 15, 18, 30, tzinfo=timezone.utc)


def test_a_session_before_0530_ist_counts_as_today():
    now = datetime(2026, 11, 16, 3, 30, tzinfo=timezone.utc)  # 09:00 IST on the 16th
    started = datetime(2026, 11, 15, 20, 30, tzinfo=timezone.utc)  # 02:00 IST on the 16th
    assert started >= _local_midnight_utc(IST, now)
    # The old UTC-midnight boundary would have excluded it.
    assert started < now.replace(hour=0, minute=0, second=0, microsecond=0)


def test_daily_series_buckets_in_tenant_zone_and_group_by_matches_select():
    day = _local_day(_Zone())
    q = select(day.label("date")).group_by(day)
    dialect = postgresql.dialect()
    sql = str(q.compile(dialect=dialect)).lower()
    assert "timezone(" in sql
    # The zone is rendered inline at execution (not a $n bind), so the SELECT
    # and GROUP BY expressions are textually identical to Postgres.
    assert sql.count("__[postcompile_tz]") == 2
    literal = str(q.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "'Asia/Kolkata'" in literal
