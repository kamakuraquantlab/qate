"""Bars, the builders that form them, and the chart that carries indicators.

A bar is a summary of a run of trades. What varies is two independent questions,
and keeping them independent is the whole design of this module:

- **When does a bar end?** After a period, a tick count, a traded volume, or a
  price range. That is a `_Closer`.
- **What is the bar, once it ends?** Plain OHLC, or Heikin-Ashi. That is a
  `_Creator`.

`BaseBarBuilder` composes one of each, so the builders below are four closers
times two creators rather than a class per combination.

## Why this was two modules

It used to be `chart.py` and `chart2.py`. The first built bars by inheritance:
`HeikinAshiBarBuilder` subclassed `CandleBarBuilder` and overrode how the bar was
made. That works until you want Heikin-Ashi bars closed by price range instead of
by period -- an entirely reasonable combination that the hierarchy simply could not
express, because the closing rule and the bar shape were the same axis.

`chart2.py` was the rewrite that separated them, and it gained
`HeikinAshiRangeBarBuilder` for free. Both modules then existed side by side with
five of six builders duplicated between them. This is the composition version, with
everything that was only in the older module -- `Bar`, `BarChart`, `DataFrameChart`
-- kept.
"""

import json
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from logging import getLogger
from typing import TYPE_CHECKING, Callable

import pandas as pd

from qate.core.model import TimeSeriesData, Trade

if TYPE_CHECKING:
    from .indicator import Indicator

LOG = getLogger(__name__)

MAX_FLOAT = float("inf")


@dataclass
class Bar(TimeSeriesData):
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    tick_count: int = 0
    open_ts: float = 0.0
    close_ts: float = 0.0

    def get_ts(self) -> float:
        return self.close_ts

    @classmethod
    def from_json(cls, json_str: str):
        obj = json.loads(json_str)
        return cls(
            obj["open"],
            obj["high"],
            obj["low"],
            obj["close"],
            obj["volume"],
            obj.get("tick_count", 0),
            obj.get("open_ts", 0),
            obj.get("close_ts", 0),
        )

    def as_dict(self):
        return {
            "ts": self.get_ts(),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "period": self.close_ts - self.open_ts,
            "volume": self.volume,
            "tick_count": self.tick_count,
            "high_low": self.high - self.low,
            "close_open": self.close - self.open,
            "avg_size": self.volume / self.tick_count if self.tick_count > 0 else 0.0,
        }


# ---------------------------------------------------------------- bar building


@dataclass
class _State:
    """The trades accumulated since the last bar closed, and their running totals."""

    data_list: list[Trade] = field(default_factory=list)
    high = 0.0
    low = MAX_FLOAT
    volume = 0.0


class _Closer(ABC):
    """Decides when the bar being accumulated has ended."""

    @abstractmethod
    def should_close(self, state: _State, trade: Trade) -> bool:
        pass


class _Creator(ABC):
    """Turns accumulated state into a bar."""

    @abstractmethod
    def create_bar(self, state: _State) -> Bar:
        pass


class TimeCloser(_Closer):
    def __init__(self, period: int):
        self.period = period

    def should_close(self, state: _State, trade: Trade) -> bool:
        if len(state.data_list) == 0:
            return False
        return int(trade.get_ts()) // self.period != int(state.data_list[-1].get_ts()) // self.period


class TickCloser(_Closer):
    def __init__(self, tick_limit: int):
        self.tick_limit = tick_limit

    def should_close(self, state: _State, trade: Trade) -> bool:
        return len(state.data_list) >= self.tick_limit


class VolumeCloser(_Closer):
    def __init__(self, volume_limit: int):
        self.volume_limit = volume_limit

    def should_close(self, state: _State, trade: Trade) -> bool:
        return state.volume >= self.volume_limit


class RangeCloser(_Closer):
    def __init__(self, box_size_rate: float):
        self._box = box_size_rate

    def should_close(self, state: _State, trade: Trade) -> bool:
        # An empty state has low=inf, so this is False until the first trade has
        # been accumulated. No guard needed.
        return (state.high - state.low) / state.low > self._box


class CandleCreator(_Creator):
    def create_bar(self, state: _State) -> Bar:
        return Bar(
            state.data_list[0].price,
            state.high,
            state.low,
            state.data_list[-1].price,
            state.volume,
            len(state.data_list),
            state.data_list[0].get_ts(),
            state.data_list[-1].get_ts(),
        )


class HeikinAshiCreator(_Creator):
    """Averaged bars: each one opens at the midpoint of the previous one.

    Carries state between bars, which is why it is an object rather than a
    function: `_open` is the running average that makes the series smooth.
    """

    def __init__(self, prev_bar: Bar = None):
        self._open = None if not prev_bar else (prev_bar.open + prev_bar.close) / 2.0

    def create_bar(self, state: _State) -> Bar:
        regular_open = state.data_list[0].price
        regular_close = state.data_list[-1].price
        regular_high = state.high
        regular_low = state.low

        ha_close = (regular_open + regular_high + regular_low + regular_close) / 4.0
        ha_open = regular_open if self._open is None else self._open
        ha_high = max(regular_high, ha_open, ha_close)
        ha_low = min(regular_low, ha_open, ha_close)

        self._open = (ha_open + ha_close) / 2.0

        return Bar(
            ha_open,
            ha_high,
            ha_low,
            ha_close,
            state.volume,
            len(state.data_list),
            state.data_list[0].get_ts(),
            state.data_list[-1].get_ts(),
        )


class BaseBarBuilder:
    """Accumulates trades and emits a bar whenever the closer says one has ended.

    `on_update` returns the completed bar, or None. Note the order: the closing
    check runs against the state *before* the incoming trade is added, so a trade
    that ends a bar belongs to the next one.
    """

    def __init__(self, closer: _Closer, creator: _Creator):
        self.closer = closer
        self.creator = creator
        self.state = _State()

    # The accumulating state was held directly on the builder before the two
    # modules were merged, and callers read it from there. Exposed as properties so
    # that stays true.
    @property
    def data_list(self) -> list[Trade]:
        return self.state.data_list

    @property
    def high(self) -> float:
        return self.state.high

    @property
    def low(self) -> float:
        return self.state.low

    @property
    def volume(self) -> float:
        return self.state.volume

    def _reset(self):
        self.state = _State()

    def _update(self, trade: Trade):
        if trade.price > self.state.high:
            self.state.high = trade.price
        if trade.price < self.state.low:
            self.state.low = trade.price
        self.state.volume += trade.size

    def on_update(self, trade: Trade) -> Bar:
        bar = None
        if self.closer.should_close(self.state, trade):
            bar = self.creator.create_bar(self.state)
            self._reset()
        self._update(trade)
        self.state.data_list.append(trade)
        return bar


class _RangeSized:
    """Box size handling, shared by the two range-closed builders."""

    def set_box_size(self, box_size_rate: float) -> None:
        self.closer = RangeCloser(box_size_rate)

    @property
    def _box(self) -> float:
        return self.closer._box

    @_box.setter
    def _box(self, box_size_rate: float) -> None:
        # `set_box_size` is the supported way. This setter exists because the box
        # size used to live on the builder itself and a strategy assigns it
        # directly; without it that assignment would land on an unused attribute
        # and the box size would silently never change.
        self.closer._box = box_size_rate


class CandleBarBuilder(BaseBarBuilder):
    def __init__(self, period: int):
        super().__init__(TimeCloser(period), CandleCreator())


class TickBarBuilder(BaseBarBuilder):
    def __init__(self, tick_limit: int):
        super().__init__(TickCloser(tick_limit), CandleCreator())


class VolumeBarBuilder(BaseBarBuilder):
    def __init__(self, volume_limit: int):
        super().__init__(VolumeCloser(volume_limit), CandleCreator())


class RangeBarBuilder(_RangeSized, BaseBarBuilder):
    def __init__(self, box_size_rate: float):
        super().__init__(RangeCloser(box_size_rate), CandleCreator())


class HeikinAshiBarBuilder(BaseBarBuilder):
    def __init__(self, period: int, prev_bar: Bar = None):
        super().__init__(TimeCloser(period), HeikinAshiCreator(prev_bar))


class HeikinAshiRangeBarBuilder(_RangeSized, BaseBarBuilder):
    """The combination the inheritance-based version could not express."""

    def __init__(self, box_size_rate: float, prev_bar: Bar = None):
        super().__init__(RangeCloser(box_size_rate), HeikinAshiCreator(prev_bar))


# ----------------------------------------------------------------------- charts


def _flatten_dict(data: dict) -> list:
    flat = []
    for a, b in data.items():
        flat.append(a)
        flat.append(b)
    return flat


class BarChart:
    def __init__(self, max_len=120, prefix=""):
        self.max_len = max_len
        self.prefix = prefix + "_" if prefix else ""
        self.data_list: deque[Bar] = deque(maxlen=max_len)
        self._indicators: dict[str, "Indicator"] = {}
        self._indicator_values: dict[str, deque] = {}

    def add_indicator(self, name: str, indicator: "Indicator"):
        """Add an indicator to the chart"""
        self._indicators[name] = indicator
        self._indicator_values[name] = deque(maxlen=self.max_len)

    def on_update(self, data: Bar):
        self.data_list.append(data)
        if not self.is_ready:
            return

        for name, indicator in self._indicators.items():
            try:
                value = indicator.compute(self.data_list)
                self._indicator_values[name].append(value)
            except Exception as e:
                LOG.error(f"Error computing indicator {name}")
                LOG.exception(e)
                # Append None to maintain alignment between bars and indicator values
                self._indicator_values[name].append(None)

    # idx=0: latest, idx=1: second latest, etc.
    def get_values(self, idx=0) -> dict:
        # No bounds checking: an out-of-range index is a logic bug, to be caught in
        # a test or a backtest rather than guarded at runtime. See
        # knowledge/01_philosophy.md §1.
        bar_data = self.data_list[-1 - idx].as_dict()
        output = bar_data.copy()
        for name, result_history in self._indicator_values.items():
            result = result_history[-1 - idx]
            if isinstance(result, dict):
                for k, v in result.items():
                    output[f"{name}:{k}"] = v
            else:
                output[name] = result
        return output

    def get_values_list(self, idx=0) -> list:
        values_dict = self.get_values(idx)
        return _flatten_dict(values_dict)

    def get_ts(self):
        return self.data_list[-1].get_ts()

    @property
    def is_ready(self) -> bool:
        return len(self.data_list) == self.max_len


class DataFrameChart:
    """Chart for indicators that want a DataFrame rather than a deque of bars.

    The whole frame is rebuilt on every bar, which is O(max_len) each time. That is
    deliberate: `max_len` is normally 5 to 120 rows, bars arrive seconds or minutes
    apart, and an immutable frame has no incremental-index state to get wrong. At
    120 rows a rebuild is around 0.1ms, so 1-second bars cost roughly 0.01% of a
    core.

    It stops being acceptable somewhere above a thousand bars or with sub-second
    bars in a busy market; `BarChart` is the one to use then.
    """

    def __init__(self, max_len=120, prefix=""):
        self.max_len = max_len
        self.prefix = prefix + "_" if prefix else ""
        self.data_list: deque[TimeSeriesData] = deque(maxlen=max_len)
        self._indicator_funcs: dict[str, Callable[[pd.DataFrame], pd.Series]] = {}
        self._indicator_values: dict[str, pd.Series] = {}

    def add_indicator(self, name: str, func: Callable[[pd.DataFrame], pd.Series]):
        self._indicator_funcs[name] = func

    def warmup(self, data: TimeSeriesData):
        self.data_list.append(data)

    def on_update(self, data: TimeSeriesData):
        self.data_list.append(data)
        df = pd.DataFrame([d.as_dict() for d in self.data_list])
        df.set_index("ts", inplace=True)

        for name, func in self._indicator_funcs.items():
            try:
                self._indicator_values[name] = func(df)
            except Exception as e:
                LOG.error(f"Error computing indicator {name}")
                LOG.exception(e)

    # idx=0: latest, idx=1: second latest, etc.
    def get_values(self, idx=0) -> dict:
        # No bounds checking -- see BarChart.get_values.
        data = self.data_list[-1 - idx].as_dict()
        result = data.copy()
        for name, series in self._indicator_values.items():
            if series is not None and not series.empty:
                result[name] = series.iloc[-1 - idx]
            else:
                result[name] = None
        return result

    def get_ts(self):
        return self.data_list[-1].get_ts()

    @property
    def is_ready(self) -> bool:
        return len(self.data_list) == self.max_len
