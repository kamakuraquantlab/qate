"""Export locally stored metrics to InfluxDB, for Grafana and ad-hoc queries.

Nothing in `qate` requires InfluxDB. A run writes its metrics to local msgpack
(`qate.store.metrics`) and finishes; this module is the optional second step
that loads them into a bucket so they can be plotted. A run that never exports
loses nothing, and an export that fails can simply be repeated -- the local
files are the record.

The connection is passed in. There is no default URL, token or organisation
here: `qate.env.sys_env.get_influxdb` reads them from the user's own config
file, and an installation without one never talks to a database.

## Timestamps

Metric objects carry float epoch seconds. Points are written at microsecond
precision, and colliding timestamps are spread by 1us each, because InfluxDB
treats measurement + tags + timestamp as a primary key and would otherwise
overwrite rather than append. Two fills inside the same millisecond are
ordinary, so this is load-bearing rather than defensive.
"""

from logging import getLogger
from pathlib import Path
from typing import Iterable

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import WriteOptions, WriteType
from influxdb_client.domain import BucketRetentionRules

from qate.core.metric import BufferedWriter
from qate.core.model import (
    DataType,
    ExchangeName,
    Field,
    Measurement,
    SettleType,
    Side,
    Tag,
)
from qate.core.order import OrderType
from qate.core.symbol import Symbol

from .metrics import read_metrics_dir

LOG = getLogger(__name__)

WRITE_TIMEOUT_MS = 120_000  # large batches from a finished backtest are slow


def create_client(url: str, token: str, org: str) -> InfluxDBClient:
    return InfluxDBClient(url, token, org=org, timeout=WRITE_TIMEOUT_MS)


def normalize_points_timestamp_us(points: list[Point]) -> None:
    """Move points to microseconds and break timestamp ties.

    Mutates in place. A point already at `WritePrecision.US` is left at its
    scale rather than multiplied again, so re-flushing a buffer after a failed
    write cannot overflow the timestamps.
    """
    last_base_us = None
    seq = 0

    for p in points:
        if p._write_precision == WritePrecision.US:
            base_us = p._time  # already microseconds -- do not multiply
        else:
            base_us = p._time * 1_000  # milliseconds -> microseconds

        if base_us == last_base_us:
            seq += 1
        else:
            seq = 0
            last_base_us = base_us

        p.time(base_us + seq, WritePrecision.US)


class InfluxdbWriter(BufferedWriter):
    """Buffered writer for one bucket, created if it does not exist."""

    def __init__(
        self,
        url: str,
        token: str,
        org: str,
        bucket_name: str,
        buffer_size: int = 2048,
        retention_seconds: int | None = None,
        param_set_id: str | None = None,
    ):
        super(InfluxdbWriter, self).__init__(buffer_size)
        self.client = create_client(url, token, org)
        self.bucket_name = bucket_name
        self.retention_seconds = retention_seconds
        self.param_set_id = param_set_id
        self._init_bucket()
        self.w = self.client.write_api(
            write_options=WriteOptions(
                write_type=WriteType.synchronous,
                batch_size=buffer_size,
            )
        )

    def _init_bucket(self):
        b = self.client.buckets_api()
        if b.find_bucket_by_name(self.bucket_name):
            return
        LOG.info(f"CREATE_BUCKET {self.bucket_name}")
        if self.retention_seconds is None:
            b.create_bucket(bucket_name=self.bucket_name)
        else:
            rules = BucketRetentionRules(type="expire", every_seconds=self.retention_seconds)
            b.create_bucket(bucket_name=self.bucket_name, retention_rules=rules)

    def write(self, points: list[Point]):
        normalize_points_timestamp_us(points)
        if self.param_set_id:
            for p in points:
                p.tag("param_set_id", self.param_set_id)
        self.w.write(self.bucket_name, record=points, write_precision=WritePrecision.US)

    def close(self):
        super().close()
        self.w.close()
        self.client.close()


def _regroup_pairs(flat: list) -> list[tuple]:
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]


_TAG_ENUMS = {
    Tag.DATA_TYPE: DataType,
    Tag.EXCHANGE_NAME: ExchangeName,
    Tag.ORDER_TYPE: OrderType,
    Tag.SETTLE_TYPE: SettleType,
    Tag.SIDE: Side,
    Tag.SYMBOL: Symbol,
}


def _get_tag_value(tag: Tag, value: int) -> str:
    enum = _TAG_ENUMS.get(tag)
    return enum(value).name if enum else str(value)


def convert_to_point(obj: list) -> Point:
    """One metric object -- `[measurement, ts, tags, fields]` -- as a Point.

    Tag and field keys arrive as enum values rather than strings, because the
    hot path packs integers. They are resolved back to names here, which is the
    only place a human ever reads them.
    """
    measurement = Measurement(obj[0])
    ts = obj[1]
    tags = _regroup_pairs(obj[2])
    fields = _regroup_pairs(obj[3])
    point = Point(measurement.name).time(int(ts * 1000), WritePrecision.MS)
    for k, v in tags:
        if type(k) is int:
            tag = Tag(k)
            k = tag.name
            v = _get_tag_value(tag, v)
        point.tag(k, v)
    for k, v in fields:
        if type(k) is int:
            k = Field(k).name
        point.field(k, v)
    return point


class InfluxdbStore:
    """A connection's worth of configuration, and the operations on it.

    `config` is the `(token, write_url, read_url)` triple
    `qate.env.sys_env.get_influxdb` returns.
    """

    def __init__(self, config: tuple[str, str, str], org: str = "qate"):
        (token, write_url, read_url) = config
        self._token = token
        self._write_url = write_url
        self._read_url = read_url
        self._org = org

    def create_writer(
        self,
        bucket_name: str,
        buffer_size: int = 2048,
        retention_seconds: int | None = None,
        param_set_id: str | None = None,
    ) -> InfluxdbWriter:
        return InfluxdbWriter(
            self._write_url,
            self._token,
            self._org,
            bucket_name,
            buffer_size,
            retention_seconds,
            param_set_id,
        )

    def delete_bucket(self, bucket_name: str) -> None:
        """Drop a bucket and everything in it."""
        client = create_client(self._write_url, self._token, self._org)
        try:
            b = client.buckets_api()
            bucket = b.find_bucket_by_name(bucket_name)
            if bucket:
                b.delete_bucket(bucket)
                LOG.info(f"DELETE_BUCKET {bucket_name}")
            else:
                LOG.info(f"DELETE_BUCKET {bucket_name} - not found, skipping")
        finally:
            client.close()

    def delete_range(self, bucket_name: str, start: str, stop: str) -> None:
        """Delete a time range from a bucket. `start`/`stop` are RFC3339."""
        client = create_client(self._write_url, self._token, self._org)
        try:
            client.delete_api().delete(
                start=start,
                stop=stop,
                predicate="",
                bucket=bucket_name,
                org=self._org,
            )
            LOG.info(f"DELETE_RANGE bucket={bucket_name} {start} -> {stop}")
        finally:
            client.close()

    def list_buckets(self) -> list[str]:
        client = create_client(self._write_url, self._token, self._org)
        try:
            return [b.name for b in client.buckets_api().find_buckets().buckets]
        finally:
            client.close()


def export_metric_objects(
    objects: Iterable[list],
    writer: InfluxdbWriter,
    batch_size: int = 2048,
) -> int:
    """Write metric objects through a writer. Returns how many were written."""
    count = 0
    points: list[Point] = []
    for obj in objects:
        points.append(convert_to_point(obj))
        count += 1
        if len(points) >= batch_size:
            writer.write(points)
            points = []
    if points:
        writer.write(points)
    return count


def export_metrics(
    directory: str | Path,
    store: InfluxdbStore,
    bucket_name: str,
    prefix: str = "",
    retention_seconds: int | None = None,
) -> int:
    """Load a directory of local msgpack metrics into a bucket.

    The second half of the local-first arrangement described in
    `qate.store.metrics`. Returns the number of metric objects written.
    """
    writer = store.create_writer(bucket_name, retention_seconds=retention_seconds)
    try:
        count = export_metric_objects(read_metrics_dir(directory, prefix), writer)
    finally:
        writer.close()
    LOG.info(f"Exported {count} metric objects from {directory} to bucket {bucket_name}")
    return count
