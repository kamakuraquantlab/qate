from dataclasses import dataclass, field
from itertools import count
from logging import getLogger

from qate.core.model import (
    Field,
    Market,
    Measurement,
    Metric,
    SettleType,
    Side,
    Tag,
    TimeSeriesData,
)
from qate.core.order import OrderResponse, OrderType
from qate.exchange import factory
from qate.exchange.registry import FeeSchedule

LOG = getLogger(__name__)
EPSILON = 0.00001


@dataclass
class PnlUpdate(TimeSeriesData):
    signal_ts: float  # When the signal that led to this trade was generated
    open_ts: float  # for offline study
    pnl: float  # Realized PnL (0 for OPEN orders)
    fee: float  # Trading fee
    settle_type: SettleType
    side: Side
    notional: float  # exec_size * exec_price
    open_cost: float  # Cost basis for this trade
    close_interval: float  # Time since last close (0 if not applicable)
    market: Market = None
    id: int = field(default_factory=count().__next__)

    def get_ts(self) -> float:
        return self.signal_ts

    @property
    def pnl_return(self) -> float:
        if self.settle_type == SettleType.OPEN:
            return 0.0
        if self.open_cost < EPSILON:
            return 0.0
        return self.pnl / self.open_cost

    def to_metric(self) -> Metric:
        if self.settle_type == SettleType.CLOSE:
            fields = [
                Field.FEE.value,
                self.fee,
                Field.PNL.value,
                self.pnl,
                "return",
                self.pnl_return,
            ]
        else:
            fields = [
                Field.FEE.value,
                self.fee,
            ]

        return Metric(
            Measurement.PNL.value,
            self.get_ts(),
            [
                Tag.EXCHANGE_NAME.value,
                self.market.exchange_name.value,
                Tag.SYMBOL.value,
                self.market.symbol.value,
                Tag.SETTLE_TYPE.value,
                self.settle_type.value,
                Tag.SIDE.value,
                self.side.value,
            ],
            fields,
        )


class PnlTracker:
    def __init__(self):
        # Long position state
        self.long_position = 0.0  # Current long size
        self.long_avg_price = 0.0  # Average entry price
        self.long_position_ts = 0.0

        # Short position state
        self.short_position = 0.0  # Current short size
        self.short_avg_price = 0.0  # Average entry price
        self.short_position_ts = 0.0

        # Realized PnL tracking
        self.realized_pnl = 0.0
        self.total_fees = 0.0

        # Tracking
        self.last_close_ts = -1.0

        # What each market this tracker sees charges, by `market.id` -- `Market` is a
        # plain dataclass and unhashable, so it cannot key a dict itself. Filled by
        # `register_fee`, and empty until something calls it: a tracker with no rates
        # costs every fill at zero.
        self.fee_rates: dict[str, FeeSchedule] = {}
        self._unknown_markets: set[str] = set()

        # Open-leg fee deferred until close so CLOSE PnlUpdate carries the full round-trip cost.
        self._pending_open_fee = 0.0

    def register_fee(self, market: Market) -> FeeSchedule | None:
        schedule = factory.get_fee_rate(market)
        if schedule is None:
            LOG.warning(
                f"No fee rate for {market.id}: its fills will be costed at zero and this "
                f"run's PnL will omit them. Install the adapter package for "
                f"{market.exchange_name.name}, or set fee_rates on its ExchangeAdapter."
            )
            return None
        self.fee_rates[market.id] = schedule
        self._unknown_markets.discard(market.id)
        LOG.info(f"Fee rate for {market.id}: maker={schedule.maker} taker={schedule.taker}")
        return schedule

    def _fee(self, market: Market, notional: float, order_type: OrderType) -> float:
        schedule = self.fee_rates.get(market.id)
        if schedule is None:
            self._warn_unknown(market)
            return 0.0
        return notional * schedule.rate(order_type)

    def _warn_unknown(self, market: Market) -> None:
        if market.id in self._unknown_markets:
            return
        self._unknown_markets.add(market.id)
        known = ", ".join(sorted(self.fee_rates)) or "none"
        LOG.warning(
            f"Costing {market.id} fills at zero: no fee rate was registered for it. "
            f"Registered: {known}. Call PnlTracker.register_fee({market.id}) at setup."
        )

    def process_order(self, order_response: OrderResponse) -> PnlUpdate:
        req = order_response.order_request

        if req.settle_type == SettleType.OPEN:
            return self._handle_open(order_response)
        else:
            return self._handle_close(order_response)

    def _handle_open(self, order_response: OrderResponse) -> PnlUpdate:
        req = order_response.order_request
        size = order_response.exec_size
        price = order_response.exec_price
        notional = size * price

        if req.side == Side.BUY:
            # Open or increase long position
            if self.long_position > EPSILON:
                # Already have long position - update weighted average
                total_cost = self.long_position * self.long_avg_price + notional
                self.long_position += size
                self.long_avg_price = total_cost / self.long_position
            else:
                # First long position
                self.long_position = size
                self.long_avg_price = price

            self.long_position_ts = req.get_ts()
            LOG.debug(
                f"OPEN LONG: size={size:.6f} price={price:.3f} "
                f"position={self.long_position:.6f} avg={self.long_avg_price:.3f}"
            )

        else:  # Side.SELL
            # Open or increase short position
            if self.short_position > EPSILON:
                # Already have short position - update weighted average
                total_cost = self.short_position * self.short_avg_price + notional
                self.short_position += size
                self.short_avg_price = total_cost / self.short_position
            else:
                # First short position
                self.short_position = size
                self.short_avg_price = price

            self.short_position_ts = req.get_ts()
            LOG.debug(
                f"OPEN SHORT: size={size:.6f} price={price:.3f} "
                f"position={self.short_position:.6f} avg={self.short_avg_price:.3f}"
            )

        # Fee is deferred: included in the next CLOSE PnlUpdate for a clear round-trip view.
        fee = self._fee(req.market, notional, req.order_type)
        self.total_fees += fee
        self._pending_open_fee += fee

        # OPEN orders don't realize PnL
        return PnlUpdate(
            signal_ts=req.get_ts(),
            open_ts=None,
            pnl=0.0,
            fee=0.0,
            settle_type=SettleType.OPEN,
            side=req.side,
            notional=notional,
            open_cost=notional,
            close_interval=0.0,
            market=req.market,
        )

    def _handle_close(self, order_response: OrderResponse) -> PnlUpdate:
        req = order_response.order_request
        size = order_response.exec_size
        price = order_response.exec_price
        notional = size * price

        if req.side == Side.BUY:
            # Closing a short position (buy back)
            if self.short_position < size - EPSILON:
                raise ValueError(
                    f"Cannot close {size:.6f} short, only have {self.short_position:.6f}. Order: {req.summary}"
                )

            # Calculate PnL: profit if we buy back cheaper than we sold
            open_ts = self.short_position_ts
            open_side = Side.SELL
            open_cost = self.short_avg_price * size
            pnl = open_cost - notional

            # Reduce short position
            self.short_position -= size
            if self.short_position < EPSILON:
                self.short_position = 0.0
                self.short_avg_price = 0.0

            LOG.debug(
                f"CLOSE SHORT: size={size:.6f} entry={self.short_avg_price:.3f} "
                f"close={price:.3f} pnl={pnl:.3f} remaining={self.short_position:.6f}"
            )

        else:  # Side.SELL
            # Closing a long position (sell out)
            if self.long_position < size - EPSILON:
                raise ValueError(
                    f"Cannot close {size:.6f} long, only have {self.long_position:.6f}. Order: {req.summary}"
                )

            # Calculate PnL: profit if we sell higher than we bought
            open_ts = self.long_position_ts
            open_side = Side.BUY
            open_cost = self.long_avg_price * size
            pnl = notional - open_cost

            # Reduce long position
            self.long_position -= size
            if self.long_position < EPSILON:
                self.long_position = 0.0
                self.long_avg_price = 0.0

            LOG.debug(
                f"CLOSE LONG: size={size:.6f} entry={self.long_avg_price:.3f} "
                f"close={price:.3f} pnl={pnl:.3f} remaining={self.long_position:.6f}"
            )

        fee = self._fee(req.market, notional, req.order_type)
        self.total_fees += fee

        # Combine with deferred open-leg fee for a round-trip view in this PnlUpdate.
        round_trip_fee = fee + self._pending_open_fee
        self._pending_open_fee = 0.0

        self.realized_pnl += pnl

        signal_ts = req.get_ts()
        close_interval = 0.0
        if self.last_close_ts > 0:
            close_interval = signal_ts - self.last_close_ts
        self.last_close_ts = signal_ts

        return PnlUpdate(
            signal_ts=signal_ts,
            open_ts=open_ts,
            pnl=pnl,
            fee=round_trip_fee,
            settle_type=SettleType.CLOSE,
            side=open_side,
            notional=notional,
            open_cost=open_cost,
            close_interval=close_interval,
            market=req.market,
        )

    def unwind_open(self, open_response: OrderResponse):
        req = open_response.order_request
        size = open_response.exec_size

        if req.side == Side.SELL:
            self.short_position -= size
            if self.short_position < EPSILON:
                self.short_position = 0.0
                self.short_avg_price = 0.0
        else:
            self.long_position -= size
            if self.long_position < EPSILON:
                self.long_position = 0.0
                self.long_avg_price = 0.0

        self.total_fees -= self._pending_open_fee
        self._pending_open_fee = 0.0
        LOG.warning(
            f"UNWIND OPEN: side={req.side.name} size={size:.6f} price={open_response.exec_price:.3f} "
            f"short_pos={self.short_position:.6f} long_pos={self.long_position:.6f}"
        )

    @property
    def net_realized(self) -> float:
        return self.realized_pnl - self.total_fees

    @property
    def has_position(self) -> bool:
        return self.long_position > EPSILON or self.short_position > EPSILON

    def get_unrealized_pnl(self, current_price: float) -> float:
        unrealized = 0.0

        if self.long_position > EPSILON:
            # Long profit: current_price > avg_price
            unrealized += (current_price - self.long_avg_price) * self.long_position

        if self.short_position > EPSILON:
            # Short profit: current_price < avg_price
            unrealized += (self.short_avg_price - current_price) * self.short_position

        return unrealized
