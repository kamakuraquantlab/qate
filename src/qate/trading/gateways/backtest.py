from collections.abc import Sequence
from itertools import count
from logging import getLogger

from qate.core.feed import MarketDataFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName, MarketPrice, OrderBook, Side
from qate.core.order import ExchangeExecution, OrderRequest, OrderTracker, OrderType
from qate.core.symbol import get_symbol_def

LOG = getLogger(__name__)

# Default slippage rate based on production trading experience
# Applied as: exec_price = market_price * (1 ± slippage_rate)
# 0.0005 = 0.05% = 5 basis points (typical for real-time simulation)
# Set to 0.0 for backtesting if slippage is applied elsewhere
DEFAULT_SLIPPAGE_RATE = 0.0005

_NO_FILLS: tuple[OrderTracker, ...] = ()


class Id:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.cnt = count()

    def next(self):
        return f"{self.prefix}.{next(self.cnt)}"


class OrderMatching:
    """Simulated orders matched against books. Publishes nothing and holds no thread.

    The gateway that owns it decides when each call runs and tells the strategy
    what happened: `BacktestGateway` inside the caller's own call, `SimulatorGateway`
    on its own loop. Not thread-safe; one thread calls it.
    """

    def __init__(self, exchange_name: ExchangeName, slippage_rate: float = 0.0, immediate_fill: bool = False):
        self.exchange_name = exchange_name
        self.slippage_rate = slippage_rate
        self.immediate_fill = immediate_fill
        self.order_id = Id(f"simulator.{exchange_name.name}.order_id")
        self.trade_id = Id(f"simulator.{exchange_name.name}.trade_id")
        self.orders: dict[int, OrderTracker] = {}
        self.order_books: dict[str, OrderBook] = {}
        self.now_ts: float | None = None

    def create(self, order_request: OrderRequest) -> OrderTracker:
        self.now_ts = order_request.ts
        order_tracker = OrderTracker(order_request, self.order_id.next(), self.now_ts)
        self.orders[order_request.ctx_id] = order_tracker
        return order_tracker

    def cancel(self, order_request: OrderRequest) -> OrderTracker | None:
        """The cancelled order, or None when it had already completed."""
        order_tracker = self.orders.pop(order_request.ctx_id, None)
        if order_tracker is None:
            return None
        order_tracker.set_cancelled(self.now_ts)
        return order_tracker

    def match(self, order_book: OrderBook) -> Sequence[OrderTracker]:
        """Fill what `order_book` crosses, and return the filled orders."""
        # Called for every book, by every backtest of a sweep: compare the
        # fields rather than build a Market to compare.
        if order_book.exchange_name is not self.exchange_name:
            return _NO_FILLS
        self.now_ts = order_book.get_ts()
        self.order_books[f"{order_book.exchange_name._name_}:{order_book.symbol._name_}"] = order_book
        filled = None
        for order_tracker in list(self.orders.values()):
            order_request = order_tracker.order_request

            # Check if we have orderbook for this market
            if order_request.market.id not in self.order_books:
                continue

            market_price = self.order_books[order_request.market.id].market_price(order_request.size)
            if not market_price:
                continue

            if order_request.order_type == OrderType.MAKER:
                exec_price = self._maker_price(order_request, market_price)
            elif order_request.order_type == OrderType.STOP:
                exec_price = self._stop_price(order_request, market_price)
            else:
                exec_price = self._taker_price(order_request, market_price)
            if exec_price is None:
                continue

            self._fill(order_tracker, exec_price)
            if filled is None:
                filled = []
            filled.append(order_tracker)
        return _NO_FILLS if filled is None else filled

    def _maker_price(self, order_request: OrderRequest, market_price: MarketPrice) -> float | None:
        # TODO implement partial match
        # TODO implement post_only
        price_unit = get_symbol_def(order_request.market.symbol).price_unit

        if order_request.side == Side.BUY:
            # BUY maker fills when our price reaches or crosses the ask
            # Use >= to account for price tick size
            if order_request.price >= market_price.ask + price_unit:
                # Fill at our limit price (maker gets price improvement)
                return market_price.ask
        else:
            # SELL maker fills when our price reaches or crosses the bid
            if order_request.price <= market_price.bid - price_unit:
                # Fill at our limit price (maker gets price improvement)
                return market_price.bid
        return None

    def _stop_price(self, order_request: OrderRequest, market_price: MarketPrice) -> float | None:
        if order_request.side == Side.BUY:
            if order_request.price > market_price.ask:
                return market_price.ask
        else:
            if order_request.price < market_price.bid:
                return market_price.bid
        return None

    def _taker_price(self, order_request: OrderRequest, market_price: MarketPrice) -> float:
        if self.immediate_fill:
            # Testing mode: fill at requested price
            return order_request.price
        # Normal mode: fill at market price + slippage
        if order_request.side == Side.BUY:
            # BUY: pay ask + slippage
            return market_price.ask * (1 + self.slippage_rate)
        # SELL: receive bid - slippage
        return market_price.bid * (1 - self.slippage_rate)

    def _fill(self, order_tracker: OrderTracker, exec_price: float):
        execution = ExchangeExecution(
            self.trade_id.next(),
            order_tracker.order_id,
            exec_price,
            order_tracker.order_request.size,
            self.now_ts,
        )
        order_tracker.on_execution(execution)
        order_tracker.set_filled(self.now_ts)
        del self.orders[order_tracker.order_request.ctx_id]


class BacktestGateway(ExchangeGateway):
    """The replay gateway: answers inside the caller's call, on the caller's thread."""

    def __init__(self, exchange_name: ExchangeName, slippage_rate: float = 0.0, immediate_fill: bool = False):
        super().__init__()
        self.matching = OrderMatching(exchange_name, slippage_rate, immediate_fill)

    @property
    def exchange_name(self) -> ExchangeName:
        return self.matching.exchange_name

    def subscribe_market_data_feed(self, market_feed: MarketDataFeed):
        market_feed.add_order_book_listener(_OrderBookSink(self))

    def handle_order_book(self, order_book: OrderBook):
        for order_tracker in self.matching.match(order_book):
            self.publish_order_filled(order_tracker.filled_response())

    def create(self, order_request: OrderRequest):
        order_tracker = self.matching.create(order_request)
        self.publish_order_created(order_tracker.created_response())

    def cancel(self, order_request: OrderRequest):
        order_tracker = self.matching.cancel(order_request)
        if order_tracker is None:
            _log_completed_cancel(order_request)
            return
        self.publish_order_cancelled(order_tracker.cancelled_response())

    def fetch_balance_sync(self, symbol):
        return {"base": 999999999.0, "quote": 999999999.0}


def _log_completed_cancel(order_request: OrderRequest) -> None:
    # The order already completed. This is the ordinary fill/cancel race,
    # not a fault: an order can be matched against a book and the strategy
    # can decide to cancel it from that same book, before the fill it
    # caused has been delivered. Live, the same race exists and the
    # exchange answers "no such order" -- so this does the equivalent and
    # lets the queued fill reconcile the strategy's view.
    #
    # It must not publish an order error either. `Strategy` treats a cancel
    # error as grounds to stop trading, which is right for a cancel the
    # venue refused and quite wrong for one that was unnecessary.
    LOG.info(f"CANCEL for an already-completed order {order_request.ctx_id}; ignoring")


class _OrderBookSink:
    def __init__(self, gateway: BacktestGateway):
        self._gateway = gateway

    def put(self, event) -> None:
        if not event:
            return
        (_event_type, order_book) = event
        self._gateway.handle_order_book(order_book)
