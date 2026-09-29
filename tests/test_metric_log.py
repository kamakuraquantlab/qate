"""The metric log: round-trip, rotation, and never destroying a finished file.

The rotation cases are the ones worth holding. A buffer can span any number of
time keys, and two earlier shapes of `write` mis-filed the events that did.
"""

import os

from qate.core.model import ExchangeName, Field, Measurement, Side, Tag
from qate.trading import metric_log as metrics
from qate.util.dt_range import DtRange

# 2026-01-15 00:00:00 and 12:00:00 local, and one second into the next day.
DAY_ONE = DtRange.from_strings("20260115", "20260115").start_ts
DAY_TWO = DtRange.from_strings("20260116", "20260116").start_ts


def metric(ts: float, price: float) -> list:
    return [
        Measurement.TRADE.value,
        ts,
        [Tag.EXCHANGE_NAME.value, ExchangeName.COINCHECK.value, Tag.SIDE.value, Side.BUY.value],
        [Field.PRICE.value, price, Field.SIZE.value, 0.01],
    ]


def test_metrics_round_trip_through_msgpack(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    writer = metrics.MetricLog(metrics.RotationInterval.ONE_DAY, 2, "Metrics")
    written = [metric(DAY_ONE + i, 100.0 + i) for i in range(5)]
    for obj in written:
        writer.add(metrics.MetricRecord(obj[1], obj))
    writer.close()

    read_back = list(metrics.read_metrics_dir(tmp_path, "Metrics"))
    assert read_back == written


def test_close_leaves_no_writing_file(tmp_path, monkeypatch):
    """A finished run must not leave a file a reader has to decide about."""
    monkeypatch.chdir(tmp_path)
    writer = metrics.MetricLog(metrics.RotationInterval.ONE_DAY, 100, "Metrics")
    writer.add(metrics.MetricRecord(DAY_ONE, metric(DAY_ONE, 1.0)))
    writer.close()

    names = sorted(os.listdir(tmp_path))
    assert names == ["Metrics_20260115.msgpack"]


def test_writing_files_are_not_read(tmp_path):
    (tmp_path / "Metrics_20260115.msgpack.writing").write_bytes(b"\x90")
    assert metrics.metrics_files(tmp_path) == []


def test_rotation_splits_on_the_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    writer = metrics.MetricLog(metrics.RotationInterval.ONE_DAY, 4, "Metrics")
    for ts in (DAY_ONE, DAY_ONE + 1, DAY_TWO, DAY_TWO + 1):
        writer.add(metrics.MetricRecord(ts, metric(ts, 1.0)))
    writer.close()

    assert sorted(os.listdir(tmp_path)) == ["Metrics_20260115.msgpack", "Metrics_20260116.msgpack"]
    assert len(list(metrics.read_metrics_dir(tmp_path, "Metrics"))) == 4


def test_one_buffer_spanning_three_days_reaches_three_files(tmp_path, monkeypatch):
    """Each object lands under its own key, however many boundaries a flush crosses."""
    monkeypatch.chdir(tmp_path)
    day_three = DtRange.from_strings("20260117", "20260117").start_ts
    stamps = [DAY_ONE, DAY_ONE + 1, DAY_TWO, DAY_TWO + 1, day_three]

    writer = metrics.MetricLog(metrics.RotationInterval.ONE_DAY, len(stamps), "Metrics")
    for ts in stamps:
        writer.add(metrics.MetricRecord(ts, metric(ts, 1.0)))
    writer.close()

    assert sorted(os.listdir(tmp_path)) == [
        "Metrics_20260115.msgpack",
        "Metrics_20260116.msgpack",
        "Metrics_20260117.msgpack",
    ]
    per_file = {p.name: len(list(metrics.read_metrics_file(p))) for p in metrics.metrics_files(tmp_path)}
    assert per_file == {
        "Metrics_20260115.msgpack": 2,
        "Metrics_20260116.msgpack": 2,
        "Metrics_20260117.msgpack": 1,
    }


def test_a_finished_file_is_never_overwritten(tmp_path, monkeypatch):
    """Recorded data cannot be recorded again, so a rename must refuse rather than clobber.

    Reachable by restart: a process writes `<key>.writing`, is restarted inside the
    same time key, appends to a fresh `<key>.writing`, and the rename that finishes
    it would land on the first run's output.
    """
    monkeypatch.chdir(tmp_path)
    existing = tmp_path / "Metrics_20260115.msgpack"
    existing.write_bytes(b"first run")

    writer = metrics.MetricLog(metrics.RotationInterval.ONE_DAY, 100, "Metrics")
    writer.add(metrics.MetricRecord(DAY_ONE, metric(DAY_ONE, 1.0)))
    writer.close()

    # Both survive: the finished file untouched, the new one still marked .writing.
    assert existing.read_bytes() == b"first run"
    assert (tmp_path / "Metrics_20260115.msgpack.writing").exists()
