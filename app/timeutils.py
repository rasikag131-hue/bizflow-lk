from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from app.security import iso_utc

LOCAL_TZ = ZoneInfo("Asia/Colombo")


def local_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def utc_bounds(start_date: date, end_date_exclusive: date) -> tuple[str, str]:
    start = datetime.combine(start_date, time.min, LOCAL_TZ).astimezone(timezone.utc)
    end = datetime.combine(end_date_exclusive, time.min, LOCAL_TZ).astimezone(timezone.utc)
    return iso_utc(start), iso_utc(end)


def date_range(period: str = "month", from_date: str | None = None, to_date: str | None = None) -> tuple[date, date]:
    today = local_today()
    if period == "today":
        return today, today + timedelta(days=1)
    if period == "week":
        start = today - timedelta(days=today.weekday())
        return start, today + timedelta(days=1)
    if period == "last_month":
        first_this = today.replace(day=1)
        last_month_end = first_this
        previous_last = first_this - timedelta(days=1)
        return previous_last.replace(day=1), last_month_end
    if period == "custom":
        try:
            start = date.fromisoformat(from_date or "")
            end_inclusive = date.fromisoformat(to_date or "")
        except ValueError:
            raise ValueError("Choose a valid start and end date.") from None
        if end_inclusive < start:
            raise ValueError("The end date must be on or after the start date.")
        if (end_inclusive - start).days > 366:
            raise ValueError("Choose a date range of 366 days or less.")
        return start, end_inclusive + timedelta(days=1)
    first = today.replace(day=1)
    return first, today + timedelta(days=1)


def local_datetime(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(LOCAL_TZ).isoformat()
    except (ValueError, AttributeError):
        return str(value)
