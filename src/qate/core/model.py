import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto

from .symbol import Symbol

EPSILON = 1e-12


class Measurement(Enum):
    TRADING = auto()
    TRADE = auto()
    MARKET_PRICE = auto()
    ORDER = auto()
    PNL = auto()
    MARKET_DATA_DELAY = auto()
    API_LATENCY = auto()
    SYS = auto()
    EVAL = auto()
    BAR = auto()


class Tag(Enum):
    EXCHANGE_NAME = auto()
    SYMBOL = auto()
    SIDE = auto()
    SETTLE_TYPE = auto()
    ORDER_TYPE = auto()
    DATA_TYPE = auto()
    DESC = auto()


class Field(Enum):
    # trade
    PRICE = auto()
    SIZE = auto()
    # market_price
    BID = auto()
    ASK = auto()
    SPREAD = auto()
    # order
    EXEC_PRICE = auto()
    EXEC_SIZE = auto()
    SLIPPAGE = auto()
    TIME_TO_CREATE = auto()
    TIME_TO_FILL = auto()
    # pnl
    PNL = auto()
    TOTAL = auto()
    RETURN = auto()
    FEE = auto()
    # general
    VALUE = auto()


class DataType(Enum):
    ORDER_BOOK = auto()
    TRADE = auto()


class TradingMode(Enum):
    DEFAULT = auto()
    NO_TRADING = auto()
    NO_ENTRY = auto()
    OPPO = auto()


class Side(Enum):
    BUY = auto()
    SELL = auto()


class SettleType(Enum):
    NONE = auto()
    OPEN = auto()
    CLOSE = auto()


class ExchangeName(Enum):
    GMO = auto()
    COINCHECK = auto()
    BITBANK = auto()
    HUOBI = auto()
    BINANCE = auto()
    RAKUTEN = auto()
    SIMULATOR = auto()


@dataclass
class Market:
    exchange_name: ExchangeName
    symbol: Symbol

    @property
    def id(self) -> str:
        return f"{self.exchange_name.name}:{self.symbol.name}"

    @classmethod
    def from_str(cls, text: str):
        parts = text.split(":")
        return cls(ExchangeName[parts[0]], Symbol[parts[1]])

    def to_str(self):
        return self.id


class TimeSeriesData(ABC):
    @abstractmethod
    def get_ts(self) -> float:
        pass


def _flatten(data: dict) -> list:
    """{"a": 1, "b": 2} -> ["a", 1, "b", 2]."""
    flat = []
    for key, value in data.items():
        flat.append(key)
        flat.append(value)
    return flat


@dataclass
class Metric(TimeSeriesData):
    """One measurement: what, when, how it is labelled, and the numbers.

    The thing a strategy emits and a metric log records. `tags` and `fields` are
    flat alternating key/value lists rather than dicts, and the keys are usually
    enum *values* rather than names, because this is written millions of times per
    run and packs straight into msgpack that way.

    That encoding is why this class exists. It used to be a bare list passed
    around positionally, so code that wanted to add a tag reached in as
    `metric_object[2].extend([...])`. `to_list` and `from_list` keep the on-disk
    form byte-identical while giving the thing a name.

    The enums behind those keys -- `Measurement`, `Tag`, `Field`, and the enums a
    tag value resolves through -- are therefore **append-only**. Reordering a
    member silently rewrites the meaning of every metric file already written.
    """

    measurement: int
    ts: float
    tags: list = field(default_factory=list)
    fields: list = field(default_factory=list)

    def get_ts(self) -> float:
        return self.ts

    def tag(self, key, value) -> "Metric":
        """Add one tag. Returns self, so it can be chained onto a producer."""
        self.tags.extend([key, value])
        return self

    def to_list(self) -> list:
        return [self.measurement, self.ts, self.tags, self.fields]

    @classmethod
    def from_list(cls, obj: list) -> "Metric":
        return cls(obj[0], obj[1], obj[2], obj[3])

    @classmethod
    def trading(cls, ts: float, fields: dict, tags: dict = None) -> "Metric":
        """A `TRADING` metric from plain dicts, for a strategy's own numbers."""
        return cls(Measurement.TRADING.value, ts, _flatten(tags or {}), _flatten(fields))


@dataclass
class Trade(TimeSeriesData):
    trade_id: str
    side: Side
    price: float
    size: float
    symbol: Symbol
    exchange_name: ExchangeName
    exchange_ts: float
    received_ts: float = time.time()

    def get_ts(self) -> float:
        return self.exchange_ts

    @property
    def market(self) -> Market:
        return Market(self.exchange_name, self.symbol)

    def to_metric(self) -> Metric:
        return Metric(
            Measurement.TRADE.value,
            self.exchange_ts,
            [
                Tag.EXCHANGE_NAME.value,
                self.exchange_name.value,
                Tag.SYMBOL.value,
                self.symbol.value,
                Tag.SIDE.value,
                self.side.value,
            ],
            [
                Field.PRICE.value,
                self.price,
                Field.SIZE.value,
                self.size,
            ],
        )


@dataclass
class MarketPrice(TimeSeriesData):
    bid: float
    ask: float
    symbol: Symbol
    exchange_name: ExchangeName
    exchange_ts: float

    def get_ts(self) -> float:
        return self.exchange_ts

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        if self.mid == 0:
            return 0
        return 10_000 * self.spread / self.mid

    def to_metric(self) -> Metric:
        return Metric(
            Measurement.MARKET_PRICE.value,
            self.exchange_ts,
            [
                Tag.EXCHANGE_NAME.value,
                self.exchange_name.value,
                Tag.SYMBOL.value,
                self.symbol.value,
            ],
            [
                Field.BID.value,
                self.bid,
                Field.ASK.value,
                self.ask,
                Field.SPREAD.value,
                self.spread,
            ],
        )


@dataclass
class OrderLevel:
    price: float
    amount: float


def _expected_execution_price(order_levels: list[OrderLevel], target_size: float) -> float:
    remaining = target_size
    notional = 0.0

    for level in order_levels:
        take = min(level.amount, remaining)
        notional += take * level.price
        remaining -= take

        if remaining <= EPSILON:
            return notional / target_size

    return None


@dataclass
class OrderBook(TimeSeriesData):
    bids: list[OrderLevel]
    asks: list[OrderLevel]
    symbol: Symbol
    exchange_name: ExchangeName
    exchange_ts: float
    received_ts: float = time.time()

    def get_ts(self) -> float:
        return self.exchange_ts

    @property
    def market(self) -> Market:
        return Market(self.exchange_name, self.symbol)

    @property
    def best_ask(self) -> float:
        if not self.asks:
            return None
        return self.asks[0].price

    @property
    def best_bid(self) -> float:
        if not self.bids:
            return None
        return self.bids[0].price

    @property
    def mid(self) -> float:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2.0

    @property
    def spread(self) -> float:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid

    @property
    def spread_bps(self) -> float:
        if self.best_bid is None or self.best_ask is None:
            return None
        return 10_000 * self.spread / self.mid

    def market_price(self, target_size: float) -> MarketPrice:
        bid = _expected_execution_price(self.bids, target_size)
        if bid is None:
            return None
        ask = _expected_execution_price(self.asks, target_size)
        if ask is None:
            return None

        return MarketPrice(bid, ask, self.symbol, self.exchange_name, self.exchange_ts)


@dataclass
class ChatMessage(TimeSeriesData):
    author: str
    channel: str
    content: str
    ts: float = time.time()

    def get_ts(self) -> float:
        return self.ts


class FatalException(Exception):
    # Raised when something unexpected happens and the event loop must stop.
    # This is an error exit — the loop logs it and notifies listeners.
    pass


class EventLoopExit(Exception):
    # Raised when the event loop should stop normally (e.g. all orders done, schedule ended).
    # This is not an error — the loop exits cleanly without logging a fault.
    # Using the exception mechanism lets any call depth unwind back to the loop
    # without threading explicit return values through every handler.
    pass
