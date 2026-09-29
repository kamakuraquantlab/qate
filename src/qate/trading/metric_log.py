"""The local record of what a strategy did: an append-only log of metric records.

A metric record is the flat list `[measurement, ts, tags, fields]` that
`qate.core.metric` and the model classes produce. `MetricLog` buffers them and
appends them to rotating local msgpack files.

Local, and nothing but local, is the design. A running strategy appends to a file
and never waits on a network, and a finished run is self-contained: everything it
emitted is on disk beside it whether or not any database is reachable. Shipping the
log somewhere -- InfluxDB for charting, say -- is a later and separate step, done by
whoever wants it, reading these files back with `read_metrics_dir`. That ordering is
the only one in which a metrics backend being down cannot lose a run.

Files rotate on a time key and carry `.writing` until the rotation closes them, so
a reader can tell a finished file from the one still being appended to.

The msgpack encoding is an implementation detail and the name deliberately does not
mention it: what this is for is keeping a record.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto
from logging import getLogger
from pathlib import Path
from typing import Iterator

import msgpack

from qate.core.metric import BufferedWriter
from qate.core.model import TimeSeriesData

LOG = getLogger(__name__)

SUFFIX = ".msgpack"
WRITING_SUFFIX = SUFFIX + ".writing"


@dataclass
class MetricRecord(TimeSeriesData):
    ts: float
    data: list

    def get_ts(self):
        return self.ts


class RotationInterval(Enum):
    ONE_HOUR = auto()
    ONE_DAY = auto()
    FIVE_MINUTE = auto()


def format_rotation_timestamp(ts: float, interval: RotationInterval) -> str:
    if interval == RotationInterval.ONE_DAY:
        return datetime.fromtimestamp(ts).strftime("%Y%m%d")
    elif interval == RotationInterval.ONE_HOUR:
        return datetime.fromtimestamp(ts).strftime("%Y%m%d_%H")
    elif interval == RotationInterval.FIVE_MINUTE:
        ts = int(ts / 300) * 300
        return datetime.fromtimestamp(ts).strftime("%Y%m%d_%H%M")
    else:
        raise ValueError(f"Unsupported interval: {interval}")


class MetricLog(BufferedWriter):
    def __init__(self, rotation_interval: RotationInterval, flush_threshold: int, prefix: str):
        super(MetricLog, self).__init__(flush_threshold)
        self.rotation_interval = rotation_interval
        self.prefix = prefix
        self.current_time_key = None
        self.current_file = None

    def _get_time_key(self, ts: float) -> str:
        return format_rotation_timestamp(ts, self.rotation_interval)

    def _close_file(self):
        if self.current_file:
            self.current_file.close()
            self.current_file = None

    def _write_objects(self, objects: list[MetricRecord]):
        packed_data = b"".join(msgpack.packb(item.data, use_bin_type=True) for item in objects)
        self.current_file.write(packed_data)

    def _finish_file(self, time_key: str) -> None:
        """Give a finished file its final name, without ever overwriting one.

        A plain rename would silently destroy an existing finished file, and that is
        reachable: a process appends to `<key>.writing`, is restarted inside the
        same time key, appends to a fresh `<key>.writing`, and the next rotation
        renames it over the first run's output. Recorded data cannot be recorded
        again, so refusing and keeping both is the only acceptable answer.
        """
        writing = Path(f"{self.prefix}_{time_key}{WRITING_SUFFIX}")
        final = Path(f"{self.prefix}_{time_key}{SUFFIX}")
        if not writing.exists():
            return
        if final.exists():
            LOG.error(
                f"Not renaming {writing} over the existing {final}: that would destroy it. "
                f"Both files are on disk; merge them by hand."
            )
            return
        writing.rename(final)

    def _rotate_file(self, time_key: str):
        self._close_file()
        if self.current_time_key:
            self._finish_file(self.current_time_key)

        self.current_time_key = time_key
        file_path = f"{self.prefix}_{time_key}{WRITING_SUFFIX}"
        LOG.info(f"Start new file {file_path}")
        self.current_file = open(file_path, "ab")

    def write(self, buffer: list[MetricRecord]):
        """Write a buffer, rotating wherever its objects cross a time key.

        Grouped by consecutive key rather than split once. A buffer can span any
        number of rotations -- a backtest replaying a year flushes thousands of
        objects at a time -- and each group has to reach the file its own key
        names. Two earlier shapes of this got it wrong: taking the key from the
        buffer's *last* object put everything before the first boundary into the
        later file, and splitting into two groups put a three-day buffer's middle
        day into the last day's file.
        """
        run: list[MetricRecord] = []
        run_key: str | None = self.current_time_key

        for item in buffer:
            time_key = self._get_time_key(item.get_ts())
            if run_key is None:
                run_key = time_key
            if time_key != run_key:
                self._write_run(run_key, run)
                run = []
                run_key = time_key
            run.append(item)

        if run:
            self._write_run(run_key, run)

    def _write_run(self, time_key: str, objects: list[MetricRecord]):
        if time_key != self.current_time_key:
            self._rotate_file(time_key)
        self._write_objects(objects)

    def close(self):
        super().close()
        self._close_file()
        # Name the last file too, so a reader does not have to decide whether a
        # `.writing` file belongs to a process that is still running.
        if self.current_time_key:
            self._finish_file(self.current_time_key)


def read_metrics_file(path: str | Path) -> Iterator[list]:
    """Every metric object in one msgpack file, in the order written."""
    with open(path, "rb") as f:
        yield from msgpack.Unpacker(f, raw=False)


def metrics_files(directory: str | Path, prefix: str = "") -> list[Path]:
    """Finished metric files under a directory, oldest name first.

    `.writing` files are skipped: they belong to a process that has not
    rotated them yet.
    """
    d = Path(directory)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.name.startswith(prefix) and p.name.endswith(SUFFIX))


def read_metrics_dir(directory: str | Path, prefix: str = "") -> Iterator[list]:
    """Every metric object under a directory, file by file."""
    for path in metrics_files(directory, prefix):
        LOG.info(f"Reading metrics from {path}")
        yield from read_metrics_file(path)
