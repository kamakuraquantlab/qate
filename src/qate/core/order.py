import math
from dataclasses import dataclass, field
from enum import Enum, auto
from itertools import count

from .model import Field, Market, Measurement, Metric, SettleType, Side, Tag, TimeSeriesData
from .symbol import get_symbol_def


class OrderType(Enum):
    DEFAULT = auto()
    MAKER = auto()
    STOP = auto()


class OrderState(Enum):
    NONE = auto()
    CREATED = auto()
    FILLED = auto()
    CANCEL_START = auto()
    CANCELLED = auto()


class OrderError(Enum):
    NONE = auto()
    CREATE = auto()
    CANCEL = auto()
    FILL = auto()


@dataclass
class OrderRequest(TimeSeriesData):
    ts: float
    market: Market
    side: Side
    price: float
    size: float
    settle_type: SettleType
    order_type: OrderType = OrderType.DEFAULT
    ctx_id: int = field(default_factory=count().__next__)
    order_id: str = ""  # for cancelling or changing orders
    close_position_id: str | None = None  # for closing orders for a specific position

    def get_ts(self) -> float:
        return self.ts

    @property
    def summary(self) -> str:
        s = f"{self.ctx_id:5d} {self.side.name} {self.settle_type.name}"
        s += f" size={self.size:.6f} price={self.price:.3f}"
        if self.close_position_id:
            s += f" close_position={self.close_position_id}"
        s += f" market={self.market.id}"
        return s


@dataclass
class OrderResponse(TimeSeriesData):
    order_request: OrderRequest
    state: OrderState = OrderState.NONE
    created_ts: float = -1.0
    order_id: str = ""
    exec_size: float = 0.0
    exec_price: float = 0.0
    position_id: str | None = None
    completed_ts: float = -1.0
    error: OrderError = OrderError.NONE

    def get_ts(self) -> float:
        return self.completed_ts if self.completed_ts > 0 else self.created_ts

    @property
    def slippage(self) -> float:
        if self.order_request.side == Side.BUY:
            return (self.exec_price - self.order_request.price) * self.exec_size
        else:
            return (self.order_request.price - self.exec_price) * self.exec_size

    @property
    def notional(self) -> float:
        return self.exec_price * self.exec_size

    @property
    def life_ts(self) -> float:
        return self.completed_ts - self.order_request.ts

    @property
    def summary(self) -> str:
        s = f"{self.state.name} {self.order_request.summary} order_id={self.order_id}"
        if self.state == OrderState.FILLED:
            s += f" exec_size={self.exec_size:.6f} exec_price={self.exec_price:.3f} slippage={self.slippage:.3f}"
            if self.position_id:
                s += f" position={self.position_id}"
            s += f" elapsed={self.life_ts:.3f}"
        return s

    def to_metric(self) -> Metric:
        order_request = self.order_request
        return Metric(
            Measurement.ORDER.value,
            self.get_ts(),
            [
                Tag.EXCHANGE_NAME.value,
                order_request.market.exchange_name.value,
                Tag.SYMBOL.value,
                order_request.market.symbol.value,
                Tag.SIDE.value,
                order_request.side.value,
                Tag.SETTLE_TYPE.value,
                order_request.settle_type.value,
                Tag.ORDER_TYPE.value,
                order_request.order_type.value,
            ],
            [
                Field.PRICE.value,
                order_request.price,
                Field.EXEC_PRICE.value,
                self.exec_price,
                Field.EXEC_SIZE.value,
                self.exec_size,
                Field.SLIPPAGE.value,
                self.slippage,
                Field.TIME_TO_CREATE.value,
                self.created_ts - order_request.ts,
                Field.TIME_TO_FILL.value,
                self.completed_ts - order_request.ts,
            ],
        )


@dataclass
class ExchangeOrder(TimeSeriesData):
    order_id: str
    exec_size: float
    exec_price: float
    state: OrderState
    exchange_ts: float
    position_id: int | None = None

    def get_ts(self) -> float:
        return self.exchange_ts


@dataclass
class ExchangeExecution(TimeSeriesData):
    trade_id: str | None
    order_id: str | None
    price: float
    size: float
    exchange_ts: float
    position_id: int | None = None

    def get_ts(self) -> float:
        return self.exchange_ts


@dataclass
class OrderTracker:
    order_request: OrderRequest
    order_id: str
    created_ts: float
    state: OrderState = OrderState.CREATED
    exec_size: float = 0.0
    cost: float = 0.0
    position_id: str | None = None
    completed_ts: float = -1.0
    retry_cnt: int = 0

    def __post_init__(self):
        self.symbol_def = get_symbol_def(self.order_request.market.symbol)
        self.last_recon_ts: float = self.created_ts

    def set_cancelled(self, ts):
        self.completed_ts = ts
        self.state = OrderState.CANCELLED

    def set_filled(self, ts):
        self.completed_ts = ts
        self.state = OrderState.FILLED
        if self.exec_size > self.order_request.size:
            self.exec_size = self.order_request.size

    def on_exchange_order(self, exchange_order: ExchangeOrder):
        # Used when CheckOrderStatus is available, e.g. bitbank
        if exchange_order.state == OrderState.CANCELLED:
            self.set_cancelled(exchange_order.get_ts())
        elif exchange_order.state == OrderState.FILLED:
            self.exec_size = exchange_order.exec_size
            self.cost = exchange_order.exec_size * exchange_order.exec_price
            if exchange_order.position_id:
                self.position_id = exchange_order.position_id
            self.set_filled(exchange_order.get_ts())

    def on_execution(self, exchange_execution: ExchangeExecution):
        # Used when private ws connection is available, e.g. GMO, COINCHECK
        self.exec_size += exchange_execution.size
        self.cost += exchange_execution.size * exchange_execution.price
        self.completed_ts = exchange_execution.get_ts()
        if exchange_execution.position_id:
            self.position_id = exchange_execution.position_id

    def is_filled(self) -> bool:
        return math.isclose(self.order_request.size, self.exec_size, abs_tol=self.symbol_def.abs_tol)

    def created_response(self) -> OrderResponse:
        return OrderResponse(
            order_request=self.order_request,
            state=OrderState.CREATED,
            created_ts=self.created_ts,
            order_id=self.order_id,
        )

    def cancelled_response(self) -> OrderResponse:
        return OrderResponse(
            order_request=self.order_request,
            state=OrderState.CANCELLED,
            created_ts=self.created_ts,
            order_id=self.order_id,
            completed_ts=self.completed_ts,
        )

    def filled_response(self) -> OrderResponse:
        return OrderResponse(
            order_request=self.order_request,
            state=OrderState.FILLED,
            created_ts=self.created_ts,
            order_id=self.order_id,
            exec_size=self.exec_size,
            exec_price=self.cost / self.exec_size if self.exec_size > 0.0 else 0.0,
            position_id=self.position_id,
            completed_ts=self.completed_ts,
        )
