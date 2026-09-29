from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum


class TimeFormat:
    YYYYMMDD = "%Y%m%d"  # user-facing input format (CLI args, filenames)
    YYYYMMDD_HHMM = "%Y%m%d_%H%M"  # filename format (msgpack rotation, CLI datetime input)
    ISO_DATE = "%Y-%m-%d"  # partition path format (date=2026-01-01)
    ISO_DATETIME = "%Y-%m-%d %H:%M:%S"  # log output format (2026-01-01 21:00:00)


class TimeAgo(Enum):
    """
    Enum for specifying relative time periods from now.
    Values are in seconds for easy calculation.
    """

    ONE_MONTH_AGO = 28 * 24 * 60 * 60  # 28 days
    TWO_WEEKS_AGO = 14 * 24 * 60 * 60
    ONE_WEEK_AGO = 7 * 24 * 60 * 60
    THREE_DAYS_AGO = 3 * 24 * 60 * 60
    TWO_DAYS_AGO = 2 * 24 * 60 * 60
    ONE_DAY_AGO = 1 * 24 * 60 * 60


@dataclass
class DtRange:
    """
    Date/time range for backtest replay.

    Supports three formats:
    1. Date format: YYYYMMDD (e.g., "20251109")
    2. DateTime format: YYYYMMDD_HHMM (e.g., "20251109_2100")
    3. TimeAgo format: ONE_MONTH_AGO, TWO_WEEKS_AGO, ONE_WEEK_AGO, THREE_DAYS_AGO

    Examples:
        # Date range: from 2025-11-09 00:00 to 2025-11-12 23:59
        DtRange("20251109", "20251112")
        DtRange("2025-11-09", "2025-11-12")

        # DateTime range: from 2025-11-09 21:00 to 2025-11-12 06:05
        DtRange("20251109_2100", "20251112_0605")
        DtRange("2025-11-09 21:00:00", "2025-11-12 06:05:59")

        # Single date: from 2025-11-09 00:00 to 2025-11-09 23:59
        DtRange("20251109", None)

        # From one month ago to now
        DtRange("ONE_MONTH_AGO", None)
    """

    _start_dt: datetime
    _end_dt: datetime
    _date_fmt: TimeFormat = TimeFormat.ISO_DATE

    def __post_init__(self):
        self._start_ts = self._start_dt.timestamp()
        self._end_ts = self._end_dt.timestamp()

    @property
    def days(self) -> list[str]:
        """Return list of ISO dates (YYYY-MM-DD) in the range, used as partition keys."""
        start_date = self._start_dt.date()
        end_date = self._end_dt.date()
        delta = end_date - start_date

        days = []
        for i in range(delta.days + 1):
            day = start_date + timedelta(days=i)
            days.append(day.strftime(self._date_fmt))
        return days

    def chunks(self, chunk_size: int) -> list["DtRange"]:
        """Splits the date range into smaller chunks."""
        if chunk_size <= 0:
            raise ValueError("Chunk size must be a positive integer.")

        chunks = []
        current_start = self._start_dt
        while current_start <= self._end_dt:
            current_end = current_start + timedelta(days=chunk_size) - timedelta(microseconds=1)
            current_end = min(current_end, self._end_dt)
            chunks.append(DtRange(current_start, current_end))
            current_start += timedelta(days=chunk_size)
        return chunks

    @property
    def start_dt(self) -> datetime:
        return self._start_dt

    @property
    def end_dt(self) -> datetime:
        return self._end_dt

    @property
    def start_ts(self) -> float:
        return self.start_dt.timestamp()

    @property
    def end_ts(self) -> float:
        return self.end_dt.timestamp()

    @property
    def start_dt_utc(self) -> datetime:
        """Return start_dt converted to UTC as a naive datetime."""
        return datetime.fromtimestamp(self._start_ts, tz=UTC).replace(tzinfo=None)

    @property
    def end_dt_utc(self) -> datetime:
        """Return end_dt converted to UTC as a naive datetime."""
        return datetime.fromtimestamp(self._end_ts, tz=UTC).replace(tzinfo=None)

    @property
    def start_str(self) -> str:
        return self._start_dt.strftime(TimeFormat.ISO_DATETIME)

    @property
    def end_str(self) -> str:
        return self._end_dt.strftime(TimeFormat.ISO_DATETIME)

    @classmethod
    def from_strings(cls, start_str: str, end_str: str | None = None) -> "DtRange":
        if _is_time_ago_format(start_str):
            return _process_time_ago(start_str)

        if len(start_str) == 19:  # YYYY-MM-DD hh:mm:ss
            return _process_datetime(start_str, end_str, TimeFormat.ISO_DATETIME)
        if len(start_str) == 10:  # YYYY-MM-DD
            return _process_datetime(start_str, end_str, TimeFormat.ISO_DATE)
        if len(start_str) == 13:  # YYYYMMDD_hhmm
            # TODO Legacy: remove this after migrating online data
            # For online market, trading data saved in msgpack files
            return _process_datetime(start_str, end_str, TimeFormat.YYYYMMDD_HHMM)
        if len(start_str) == 8:  # YYYYMMDD
            # TODO Legacy: remove this after migrating online data
            # For online market, trading data saved in msgpack files
            return _process_datetime(start_str, end_str, TimeFormat.YYYYMMDD)

        raise Exception("Unexpected input date str: " + start_str)

    def shift_hours(self, hours: int) -> "DtRange":
        return DtRange(
            self._start_dt + timedelta(hours=hours),
            self._end_dt + timedelta(hours=hours),
        )

    def is_within(self, ts: float) -> bool:
        return self._start_ts <= ts <= self._end_ts


def _is_time_ago_format(time_ago_str: str) -> bool:
    return time_ago_str in [member.name for member in TimeAgo]


def _process_time_ago(time_ago_str: str) -> DtRange:
    time_ago_enum = TimeAgo[time_ago_str]
    seconds_ago = time_ago_enum.value
    now = datetime.now()
    return DtRange(now - timedelta(seconds=seconds_ago), now)


def _process_datetime(start_str: str, end_str: str, time_format: TimeFormat) -> DtRange:
    start_dt = datetime.strptime(start_str, time_format)
    if end_str:
        end_dt = datetime.strptime(end_str, time_format)
        if time_format == TimeFormat.ISO_DATE or time_format == TimeFormat.YYYYMMDD:
            end_dt = end_dt.replace(hour=23, minute=59, second=59, microsecond=999_999)
        elif time_format == TimeFormat.ISO_DATETIME:
            end_dt = end_dt.replace(microsecond=999_999)
        elif time_format == TimeFormat.YYYYMMDD_HHMM:
            end_dt = end_dt.replace(second=59, microsecond=999_999)
    else:
        if time_format == TimeFormat.ISO_DATE or time_format == TimeFormat.YYYYMMDD:
            # end of same day, in this case we want to select the data for 1 day
            end_dt = start_dt.replace(hour=23, minute=59, second=59, microsecond=999_999)
        else:
            end_dt = datetime.now()
    return DtRange(start_dt, end_dt)
