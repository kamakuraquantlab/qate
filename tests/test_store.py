"""The local-first store: metrics round-trip, parquet partitions, point conversion.

The InfluxDB half is tested only as far as the conversion. Writing needs a
server, and needing one is exactly what the local files exist to avoid.
"""

import os

import pytest

from qate.core.model import ExchangeName, Field, Market, Measurement, Side, Tag
from qate.core.symbol import Symbol
from qate.store import metrics, timeseries
from qate.util.dt_range import DtRange

MARKET = Market(ExchangeName.COINCHECK, Symbol.BTC_SPOT)

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
    writer = metrics.MsgpackWriter(metrics.RotationInterval.ONE_DAY, 2, "Metrics")
    written = [metric(DAY_ONE + i, 100.0 + i) for i in range(5)]
    for obj in written:
        writer.add(metrics.WriterObject(obj[1], obj))
    writer.close()

    read_back = list(metrics.read_metrics_dir(tmp_path, "Metrics"))
    assert read_back == written


def test_close_leaves_no_writing_file(tmp_path, monkeypatch):
    """A finished run must not leave a file a reader has to decide about."""
    monkeypatch.chdir(tmp_path)
    writer = metrics.MsgpackWriter(metrics.RotationInterval.ONE_DAY, 100, "Metrics")
    writer.add(metrics.WriterObject(DAY_ONE, metric(DAY_ONE, 1.0)))
    writer.close()

    names = sorted(os.listdir(tmp_path))
    assert names == ["Metrics_20260115.msgpack"]


def test_writing_files_are_not_read(tmp_path):
    (tmp_path / "Metrics_20260115.msgpack.writing").write_bytes(b"\x90")
    assert metrics.metrics_files(tmp_path) == []


def test_rotation_splits_on_the_day(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    writer = metrics.MsgpackWriter(metrics.RotationInterval.ONE_DAY, 4, "Metrics")
    for ts in (DAY_ONE, DAY_ONE + 1, DAY_TWO, DAY_TWO + 1):
        writer.add(metrics.WriterObject(ts, metric(ts, 1.0)))
    writer.close()

    assert sorted(os.listdir(tmp_path)) == ["Metrics_20260115.msgpack", "Metrics_20260116.msgpack"]
    assert len(list(metrics.read_metrics_dir(tmp_path, "Metrics"))) == 4


def test_one_buffer_spanning_three_days_reaches_three_files(tmp_path, monkeypatch):
    """Each object lands under its own key, however many boundaries a flush crosses."""
    monkeypatch.chdir(tmp_path)
    day_three = DtRange.from_strings("20260117", "20260117").start_ts
    stamps = [DAY_ONE, DAY_ONE + 1, DAY_TWO, DAY_TWO + 1, day_three]

    writer = metrics.MsgpackWriter(metrics.RotationInterval.ONE_DAY, len(stamps), "Metrics")
    for ts in stamps:
        writer.add(metrics.WriterObject(ts, metric(ts, 1.0)))
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


def test_timeseries_writes_one_file_per_day(tmp_path):
    store = timeseries.StrategyStore(tmp_path, "Pnl", MARKET, "example.v1", "abc123")
    store.add(DAY_ONE, {"pnl_inc": 1.5})
    store.add(DAY_TWO, {"pnl_inc": -0.5})
    store.close()

    expected = tmp_path / "dataset=Pnl" / "strategy=example.v1" / "exchange=COINCHECK" / "symbol=BTC_SPOT" / "param_id=abc123"
    assert sorted(p.name for p in expected.glob("date=*")) == ["date=2026-01-15", "date=2026-01-16"]

    df = store.read(["2026-01-15", "2026-01-16"])
    assert list(df["pnl_inc"]) == [1.5, -0.5]
    assert list(df["ts"]) == [DAY_ONE, DAY_TWO]


def test_timeseries_read_trims_to_the_exact_range(tmp_path):
    store = timeseries.SingleMarketStore(tmp_path, "Pnl", MARKET)
    for offset in (0, 3600, 7200):
        store.add(DAY_ONE + offset, {"v": offset})
    store.close()

    dt_range = DtRange.from_strings("20260115_0030", "20260115_0130")
    assert list(store.read(dt_range)["v"]) == [3600]


def test_data_check_reports_missing_partitions(tmp_path):
    store = timeseries.SingleMarketStore(tmp_path, "Pnl", MARKET)
    store.add(DAY_ONE, {"v": 1})
    store.close()

    check = store.data_check(DtRange.from_strings("20260115", "20260116"))
    assert check["num_days"] == 2
    assert check["num_files"] == 1
    assert check["missing_files"] == ["2026-01-16"]


def test_result_store_keeps_pnl_and_bars_apart(tmp_path):
    results = timeseries.ResultStore(tmp_path, "example.v1", "abc123")
    assert results.create_pnl(MARKET).dataset == timeseries.ResultStore.PNL
    assert results.create_bar(MARKET).dataset == timeseries.ResultStore.BAR


def test_convert_to_point_resolves_enum_keys():
    influx = pytest.importorskip("qate.store.influx")
    point = influx.convert_to_point(metric(DAY_ONE, 123.0))
    line = point.to_line_protocol()
    assert line.startswith("TRADE,")
    assert "EXCHANGE_NAME=COINCHECK" in line
    assert "SIDE=BUY" in line
    assert "PRICE=123" in line


def test_colliding_timestamps_are_spread_by_one_microsecond():
    """Influx overwrites on an identical key; two fills in a millisecond are normal."""
    influx = pytest.importorskip("qate.store.influx")
    points = [influx.convert_to_point(metric(DAY_ONE, float(i))) for i in range(3)]
    influx.normalize_points_timestamp_us(points)
    times = [p._time for p in points]
    assert times == [times[0], times[0] + 1, times[0] + 2]


def test_normalize_is_idempotent():
    """A re-flushed buffer after a failed write must not drift."""
    influx = pytest.importorskip("qate.store.influx")
    points = [influx.convert_to_point(metric(DAY_ONE, 1.0))]
    influx.normalize_points_timestamp_us(points)
    once = points[0]._time
    influx.normalize_points_timestamp_us(points)
    assert points[0]._time == once
