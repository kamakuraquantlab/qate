from logging import getLogger

from qate.core.features import FEATURES, Feature
from qate.core.model import Market, SettleType, Side
from qate.core.order import OrderRequest, OrderResponse
from qate.trading.exceptions import StopTradingException

from .pnl_tracker import PnlTracker, PnlUpdate

LOG = getLogger(__name__)
EPSILON = 0.00001
SLIPPAGE_FACTOR = 1.02  # Safety margin for resource checks


class Inventory:
    def __init__(self, market: Market, max_drawdown: float, pnl_tracker: PnlTracker = None):
        self.market = market
        self.max_drawdown = max_drawdown
        self.tracker = pnl_tracker if pnl_tracker is not None else PnlTracker()
        self.tracker.register_fee(market)

        # Peak PnL tracking for drawdown calculation
        self._peak_pnl = 0.0

    def process_order(self, order_response: OrderResponse) -> PnlUpdate:
        pnl_update = self.tracker.process_order(order_response)
        self._health_check()
        return pnl_update

    def _health_check(self):
        if not FEATURES.get(Feature.INVENTORY_HEALTH_CHECK):
            return

        net_pnl = self.tracker.net_realized
        self._peak_pnl = max(self._peak_pnl, net_pnl)

        drawdown = self._peak_pnl - net_pnl
        if drawdown > self.max_drawdown:
            raise StopTradingException(
                f"Max drawdown exceeded: peak={self._peak_pnl:.2f} "
                f"current={net_pnl:.2f} drawdown={drawdown:.2f} "
                f"limit={self.max_drawdown:.2f}"
            )

    @property
    def log_str(self):
        raise NotImplementedError


class SpotInventory(Inventory):
    def __init__(
        self,
        market: Market,
        quote: float,
        base: float,
        max_drawdown: float,
        pnl_tracker: PnlTracker = None,
    ):
        super().__init__(market, max_drawdown, pnl_tracker)
        self.quote = quote  # Quote currency balance (e.g., JPY)
        self.base = base  # Base currency balance (e.g., BTC)
        self.reserved_quote = 0.0  # Reserved for pending orders
        self.reserved_base = 0.0  # Reserved for pending orders

    def reserve(self, order_request: OrderRequest):
        if order_request.side == Side.BUY:
            self.reserved_quote += order_request.size * order_request.price
        else:
            self.reserved_base += order_request.size

    def release(self, order_request: OrderRequest):
        if order_request.side == Side.BUY:
            self.reserved_quote -= order_request.size * order_request.price
        else:
            self.reserved_base -= order_request.size

    def process_order(self, order_response: OrderResponse) -> PnlUpdate:
        pnl_update = super().process_order(order_response)
        notional = order_response.exec_size * order_response.exec_price
        self.quote -= pnl_update.fee
        if order_response.order_request.side == Side.BUY:
            self.quote -= notional
            self.base += order_response.exec_size
        else:
            self.quote += notional
            self.base -= order_response.exec_size

        return pnl_update

    @property
    def available_quote(self) -> float:
        return self.quote - self.reserved_quote

    @property
    def available_base(self) -> float:
        return self.base - self.reserved_base

    def can_buy(self, size: float, price: float) -> bool:
        required = size * price * SLIPPAGE_FACTOR
        return self.available_quote >= required

    def can_sell(self, size: float) -> bool:
        return self.available_base >= size + EPSILON

    @property
    def log_str(self):
        return (
            f"SpotInventory(quote={self.quote:.2f}, base={self.base:.6f}, "
            f"reserved_quote={self.reserved_quote:.2f}, reserved_base={self.reserved_base:.6f}, "
            f"pnl={self.tracker.net_realized:.2f})"
        )


class LeverageInventory(Inventory):
    def __init__(
        self,
        market: Market,
        initial_margin: float,
        max_drawdown: float,
        leverage: float = 2.0,
        pnl_tracker: PnlTracker = None,
    ):
        super().__init__(market, max_drawdown, pnl_tracker)
        self._initial_margin = initial_margin
        self.leverage = leverage
        self._reserved_margin = 0.0
        self._reserved_short_position = 0.0
        self._reserved_long_position = 0.0

    @property
    def margin(self) -> float:
        return self._initial_margin + self.tracker.net_realized - self._reserved_margin

    def reserve(self, order_request: OrderRequest):
        if order_request.settle_type == SettleType.OPEN:
            self._reserved_margin += order_request.size * order_request.price
        elif order_request.settle_type == SettleType.CLOSE:
            if order_request.side == Side.BUY:
                self._reserved_short_position += order_request.size
            else:
                self._reserved_long_position += order_request.size

    def release(self, order_request: OrderRequest):
        if order_request.settle_type == SettleType.OPEN:
            self._reserved_margin -= order_request.size * order_request.price
        elif order_request.settle_type == SettleType.CLOSE:
            if order_request.side == Side.BUY:
                self._reserved_short_position -= order_request.size
            else:
                self._reserved_long_position -= order_request.size

    def can_open(self, size: float, price: float) -> bool:
        position_value = size * price
        required_margin = (position_value / self.leverage) * SLIPPAGE_FACTOR
        return self.margin >= required_margin

    def can_close(self, side: Side, size: float) -> bool:
        if side == Side.BUY:
            return self.tracker.short_position - self._reserved_short_position >= size - EPSILON
        else:
            return self.tracker.long_position - self._reserved_long_position >= size - EPSILON

    @property
    def log_str(self):
        return (
            f"LeverageInventory(margin={self.margin:.2f}, "
            f"peak={self._peak_pnl:.2f}, pnl={self.tracker.net_realized:.2f}, "
            f"long={self.tracker.long_position:.4f}, short={self.tracker.short_position:.4f})"
        )

    def has_reserved_positions(self) -> bool:
        return self._reserved_long_position > EPSILON or self._reserved_short_position > EPSILON
