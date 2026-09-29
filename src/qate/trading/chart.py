import json
from collections import deque
from dataclasses import dataclass
from logging import getLogger
from typing import TYPE_CHECKING, Callable

import pandas as pd

from qate.core.model import TimeSeriesData, Trade

if TYPE_CHECKING:
    from .indicator import Indicator

LOG = getLogger(__name__)


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


MAX_FLOAT = float("inf")


class BaseBarBuilder:
    def __init__(self):
        self.data_list: list[Trade] = []
        self._reset()

    def _reset(self):
        self.high = 0.0
        self.low = MAX_FLOAT
        self.volume = 0.0
        self.data_list = []

    def _check_close(self, trade: Trade) -> bool:
        pass

    def _update(self, trade: Trade):
        if trade.price > self.high:
            self.high = trade.price
        if trade.price < self.low:
            self.low = trade.price
        self.volume += trade.size

    def on_update(self, trade: Trade) -> Bar:
        bar = None
        if self._check_close(trade):
            bar = self._build_bar()
            self._reset()
        self._update(trade)
        self.data_list.append(trade)
        return bar

    def _build_bar(self) -> Bar:
        return Bar(
            self.data_list[0].price,
            self.high,
            self.low,
            self.data_list[-1].price,
            self.volume,
            len(self.data_list),
            self.data_list[0].get_ts(),
            self.data_list[-1].get_ts(),
        )


class CandleBarBuilder(BaseBarBuilder):
    def __init__(self, period: int):
        super(CandleBarBuilder, self).__init__()
        self.period = period

    def _check_close(self, trade: Trade) -> bool:
        if len(self.data_list) == 0:
            return False
        return int(trade.get_ts()) // self.period != int(self.data_list[-1].get_ts()) // self.period


class HeikinAshiBarBuilder(CandleBarBuilder):
    def __init__(self, period: int, prev_bar: Bar = None):
        super(HeikinAshiBarBuilder, self).__init__(period)
        self._open = None if not prev_bar else (prev_bar.open + prev_bar.close) / 2.0

    def _build_bar(self) -> Bar:
        # Regular OHLC values from trades
        regular_open = self.data_list[0].price
        regular_close = self.data_list[-1].price
        regular_high = self.high
        regular_low = self.low

        # Calculate Heikin-Ashi values using correct formulas
        ha_close = (regular_open + regular_high + regular_low + regular_close) / 4.0
        ha_open = regular_open if self._open is None else self._open
        ha_high = max(regular_high, ha_open, ha_close)
        ha_low = min(regular_low, ha_open, ha_close)

        # Update self._open for next bar calculation
        self._open = (ha_open + ha_close) / 2.0

        return Bar(
            ha_open,
            ha_high,
            ha_low,
            ha_close,
            self.volume,
            len(self.data_list),
            self.data_list[0].get_ts(),
            self.data_list[-1].get_ts(),
        )


class TickBarBuilder(BaseBarBuilder):
    def __init__(self, tick_limit: int):
        super(TickBarBuilder, self).__init__()
        self.tick_limit = tick_limit

    def _check_close(self, _: Trade) -> bool:
        return len(self.data_list) >= self.tick_limit


class VolumeBarBuilder(BaseBarBuilder):
    def __init__(self, volume_limit: int):
        super(VolumeBarBuilder, self).__init__()
        self.volume_limit = volume_limit

    def _check_close(self, _: Trade) -> bool:
        return self.volume >= self.volume_limit


class RangeBarBuilder(BaseBarBuilder):
    def __init__(self, box_size_rate: float):
        super(RangeBarBuilder, self).__init__()
        self._box = box_size_rate

    def set_box_size(self, box_size_rate: float):
        self._box = box_size_rate

    def _check_close(self, _: Trade) -> bool:
        return (self.high - self.low) / self.low > self._box


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
        # No bounds checking - this is a static error that should be caught in tests/backtest
        # See README.md "Design Philosophy: Error Handling" for rationale
        bar_data = self.data_list[-1 - idx].as_dict()
        output = bar_data.copy()
        for name, result_history in self._indicator_values.items():
            result = result_history[-1 - idx]  # No bounds checking - see above
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
    """
    Pandas-based chart for indicators that require DataFrame operations.

    PERFORMANCE NOTE (Issue #5 from CHART_ISSUES.md):
    This class rebuilds the entire DataFrame from scratch on every bar update (line 244).
    This is O(n) time and memory where n = max_len (default 120).

    Why this is acceptable for typical use cases:
    - max_len is usually small (5-120 bars)
    - Bar updates are infrequent (e.g., every 50 trades, every 60 seconds)
    - Simplicity: DataFrame is immutable, no state consistency issues
    - Pandas operations are fast for small DataFrames (120 rows is negligible)

    When this might become a bottleneck:
    - Very large max_len (>1000 bars)
    - Very high frequency updates (1-second candles in high-volume markets)
    - Many indicators with complex calculations

    Example performance cost:
    - max_len=120, 1-second bars: 3600 DataFrame rebuilds per hour
    - Each rebuild: ~0.1ms for 120 rows → 360ms/hour overhead (~0.01% CPU)
    - max_len=1000, 1-second bars: ~3ms per rebuild → ~11 seconds/hour (~0.3% CPU)

    Alternatives considered but rejected for simplicity:
    - Use pd.concat() to append rows: Still O(n) memory copy, more complex
    - Maintain incremental DataFrame: Requires careful index management
    - Use numpy arrays: Loses pandas convenience for indicators

    Decision: Keep current implementation. The overhead is acceptable for typical
    trading strategies. If needed, use BarChart instead for better performance.
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
        # No bounds checking - this is a static error that should be caught in tests/backtest
        # See README.md "Design Philosophy: Error Handling" for rationale
        data = self.data_list[-1 - idx].as_dict()
        result = data.copy()
        for name, series in self._indicator_values.items():
            if series is not None and not series.empty:
                result[name] = series.iloc[-1 - idx]  # No bounds checking - see above
            else:
                result[name] = None
        return result

    def get_ts(self):
        return self.data_list[-1].get_ts()

    @property
    def is_ready(self) -> bool:
        return len(self.data_list) == self.max_len
