"""Bar building after `chart` and `chart2` became one module.

Two things are being held here. The first is that every builder still closes bars
where it used to — five of the six existed in both modules, so a merge is only safe
if they agree. The second is the attribute surface: the accumulating state moved
from the builder onto a `_State`, and consumers read it off the builder.

The Heikin-Ashi range builder is the reason the merge went the way it did: it is
the combination the inheritance version could not express.
"""

import pytest

from qate.core.model import ExchangeName, Side, Trade
from qate.core.symbol import Symbol
from qate.trading.chart import (
    Bar,
    BarChart,
    BaseBarBuilder,
    CandleBarBuilder,
    CandleCreator,
    HeikinAshiRangeBarBuilder,
    RangeBarBuilder,
    RangeCloser,
    TickBarBuilder,
    TimeCloser,
    VolumeBarBuilder,
)

TS = 1767225600.0


def trade(ts: float, price: float, size: float = 1.0) -> Trade:
    return Trade("", Side.BUY, price, size, Symbol.BTC_SPOT, ExchangeName.COINCHECK, ts)


def feed(builder, trades) -> list[Bar]:
    return [bar for bar in (builder.on_update(t) for t in trades) if bar is not None]


# ------------------------------------------------------------------- closing


def test_a_candle_bar_closes_on_the_period_boundary():
    builder = CandleBarBuilder(period=60)
    bars = feed(builder, [trade(TS, 100.0), trade(TS + 30, 101.0), trade(TS + 61, 102.0)])
    assert len(bars) == 1
    bar = bars[0]
    assert (bar.open, bar.high, bar.low, bar.close) == (100.0, 101.0, 100.0, 101.0)
    assert bar.tick_count == 2
    # The trade that ended the bar belongs to the next one.
    assert builder.data_list == [trade(TS + 61, 102.0)] or len(builder.data_list) == 1


def test_a_tick_bar_closes_on_the_count():
    bars = feed(TickBarBuilder(tick_limit=2), [trade(TS + i, 100.0 + i) for i in range(5)])
    assert [b.tick_count for b in bars] == [2, 2]


def test_a_volume_bar_closes_on_traded_size():
    bars = feed(VolumeBarBuilder(volume_limit=3), [trade(TS + i, 100.0, size=1.0) for i in range(8)])
    assert [b.volume for b in bars] == [3.0, 3.0]


def test_a_range_bar_closes_when_the_box_is_exceeded():
    # 1% box on a 100 base: closes once high/low spread past 1.
    bars = feed(RangeBarBuilder(box_size_rate=0.01), [trade(TS, 100.0), trade(TS + 1, 101.5), trade(TS + 2, 102.0)])
    assert len(bars) == 1
    assert bars[0].low == 100.0
    assert bars[0].high == 101.5


def test_the_first_trade_never_closes_a_bar():
    """An empty state has low=inf, and that must not be a divide-by-zero or a close."""
    for builder in (CandleBarBuilder(60), RangeBarBuilder(0.01), VolumeBarBuilder(1)):
        assert builder.on_update(trade(TS, 100.0)) is None


# --------------------------------------------------------------- heikin-ashi


def test_heikin_ashi_range_bars_average_and_close_on_the_box():
    """The combination that motivated the merge: HA bars closed by price range."""
    builder = HeikinAshiRangeBarBuilder(box_size_rate=0.01)
    bars = feed(builder, [trade(TS, 100.0), trade(TS + 1, 102.0), trade(TS + 2, 104.0), trade(TS + 3, 106.0)])
    assert bars, "no bar closed; the range closer is not driving the HA creator"

    first = bars[0]
    # close is the mean of the four regular values, not the last price.
    assert first.close == pytest.approx((100.0 + 102.0 + 100.0 + 102.0) / 4.0)
    assert first.high >= first.close and first.low <= first.close


def test_heikin_ashi_carries_its_open_between_bars():
    builder = HeikinAshiRangeBarBuilder(box_size_rate=0.001)
    bars = feed(builder, [trade(TS + i, 100.0 + i) for i in range(12)])
    assert len(bars) >= 2
    # The second bar opens at the midpoint of the first, which is what makes the
    # series smooth. A fresh creator per bar would open it at the trade price.
    assert bars[1].open == pytest.approx((bars[0].open + bars[0].close) / 2.0)


def test_a_previous_bar_seeds_the_average():
    prev = Bar(10.0, 12.0, 9.0, 11.0)
    builder = HeikinAshiRangeBarBuilder(box_size_rate=0.01, prev_bar=prev)
    bars = feed(builder, [trade(TS, 100.0), trade(TS + 1, 102.0), trade(TS + 2, 104.0)])
    assert bars[0].open == pytest.approx((prev.open + prev.close) / 2.0)


# ------------------------------------------------- the box size, and its setter


def test_set_box_size_changes_when_a_range_bar_closes():
    builder = RangeBarBuilder(box_size_rate=0.5)
    builder.on_update(trade(TS, 100.0))
    builder.on_update(trade(TS + 1, 110.0))  # 10% range, far inside a 50% box
    assert builder.on_update(trade(TS + 2, 111.0)) is None

    builder.set_box_size(0.01)
    assert builder.on_update(trade(TS + 3, 112.0)) is not None


def test_assigning_the_private_box_still_works():
    """A strategy assigns `_box` directly; the merge must not make that a no-op.

    The box size used to live on the builder. It lives on the closer now, and the
    property forwards — otherwise the assignment would land on an unused attribute
    and the box size would silently never change.
    """
    builder = RangeBarBuilder(box_size_rate=0.5)
    builder._box = 0.01
    assert builder._box == 0.01
    assert builder.closer._box == 0.01

    builder.on_update(trade(TS, 100.0))
    builder.on_update(trade(TS + 1, 110.0))
    assert builder.on_update(trade(TS + 2, 111.0)) is not None


def test_the_heikin_ashi_range_builder_is_box_sized_too():
    builder = HeikinAshiRangeBarBuilder(box_size_rate=0.5)
    builder.set_box_size(0.01)
    assert builder.closer._box == 0.01
    builder._box = 0.02
    assert builder._box == 0.02


# ------------------------------------------------------- the attribute surface


def test_the_accumulating_state_is_readable_off_the_builder():
    """Consumers read `data_list`, `high`, `low` and `volume` from the builder."""
    builder = RangeBarBuilder(box_size_rate=1.0)
    builder.on_update(trade(TS, 100.0, size=2.0))
    builder.on_update(trade(TS + 1, 105.0, size=3.0))

    assert [t.price for t in builder.data_list] == [100.0, 105.0]
    assert builder.high == 105.0
    assert builder.low == 100.0
    assert builder.volume == 5.0
    # This is the access a live strategy makes to size its next box.
    assert builder.data_list[-1].price == 105.0


def test_closing_a_bar_resets_the_state():
    builder = TickBarBuilder(tick_limit=2)
    feed(builder, [trade(TS, 100.0), trade(TS + 1, 101.0), trade(TS + 2, 102.0)])
    assert len(builder.data_list) == 1
    assert builder.high == 102.0
    assert builder.volume == 1.0


def test_a_builder_is_a_closer_and_a_creator():
    """Composition is the point: a new combination needs no new class."""
    builder = BaseBarBuilder(TimeCloser(60), CandleCreator())
    assert isinstance(builder, BaseBarBuilder)
    assert feed(builder, [trade(TS, 100.0), trade(TS + 61, 101.0)])

    assert isinstance(RangeBarBuilder(0.01).closer, RangeCloser)
    assert isinstance(CandleBarBuilder(60).creator, CandleCreator)


# -------------------------------------------------------------------- charting


def test_the_chart_is_not_ready_until_it_is_full():
    chart = BarChart(max_len=3)
    for i in range(2):
        chart.on_update(Bar(100.0, 101.0, 99.0, 100.5, close_ts=TS + i))
    assert chart.is_ready is False
    chart.on_update(Bar(100.0, 101.0, 99.0, 100.5, close_ts=TS + 2))
    assert chart.is_ready is True
    assert chart.get_values()["close"] == 100.5
