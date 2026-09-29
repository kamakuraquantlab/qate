from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from logging import getLogger

from qate.core.model import Trade

from .chart import Bar

LOG = getLogger(__name__)
MAX_FLOAT = float("inf")


@dataclass
class _State:
    data_list: list[Trade] = field(default_factory=list)
    high = 0.0
    low = MAX_FLOAT
    volume = 0.0


class _Closer(ABC):
    @abstractmethod
    def should_close(self, state: _State, trade: Trade) -> bool:
        pass


class _Creator(ABC):
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
    def __init__(self, prev_bar: Bar = None):
        self._open = None if not prev_bar else (prev_bar.open + prev_bar.close) / 2.0

    def create_bar(self, state: _State) -> Bar:
        # Regular OHLC values from trades
        regular_open = state.data_list[0].price
        regular_close = state.data_list[-1].price
        regular_high = state.high
        regular_low = state.low

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
            state.volume,
            len(state.data_list),
            state.data_list[0].get_ts(),
            state.data_list[-1].get_ts(),
        )


class BaseBarBuilder:
    def __init__(self, closer: _Closer, creator: _Creator):
        self.closer = closer
        self.creator = creator
        self.state = _State()

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


class CandleBarBuilder(BaseBarBuilder):
    def __init__(self, period: int):
        super().__init__(TimeCloser(period), CandleCreator())


class TickBarBuilder(BaseBarBuilder):
    def __init__(self, tick_limit: int):
        super().__init__(TickCloser(tick_limit), CandleCreator())


class VolumeBarBuilder(BaseBarBuilder):
    def __init__(self, volume_limit: int):
        super().__init__(VolumeCloser(volume_limit), CandleCreator())


class RangeBarBuilder(BaseBarBuilder):
    def __init__(self, box_size_rate: float):
        super().__init__(RangeCloser(box_size_rate), CandleCreator())

    def set_box_size(self, box_size_rate: float):
        self.closer = RangeCloser(box_size_rate)


class HeikinAshiBarBuilder(BaseBarBuilder):
    def __init__(self, period: int, prev_bar: Bar = None):
        super().__init__(TimeCloser(period), HeikinAshiCreator(prev_bar))


class HeikinAshiRangeBarBuilder(BaseBarBuilder):
    def __init__(self, box_size_rate: float, prev_bar: Bar = None):
        super().__init__(RangeCloser(box_size_rate), HeikinAshiCreator(prev_bar))

    def set_box_size(self, box_size_rate: float):
        self.closer = RangeCloser(box_size_rate)
