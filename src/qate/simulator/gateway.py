from itertools import count
from logging import getLogger

from qate.core.ev_type import EventType
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


class Id:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.cnt = count()

    def next(self):
        return f"{self.prefix}.{next(self.cnt)}"


class SimulatorGateway(ExchangeGateway):
    def __init__(self, exchange_name: ExchangeName, slippage_rate: float = 0.0, immediate_fill: bool = False):
        """
        Initialize simulator gateway.

        Args:
            exchange_name: Exchange to simulate
            slippage_rate: Slippage rate for taker orders (default 0.0 = no slippage)
                          For real-time simulation, use 0.0005 (5 basis points)
                          For backtesting, use 0.0 if slippage is applied elsewhere
            immediate_fill: If True, fill taker orders at requested price (for testing only)
                           Default False uses market price + slippage
        """
        super(SimulatorGateway, self).__init__(None)
        self._exchange_name = exchange_name
        self.slippage_rate = slippage_rate
        self.immediate_fill = immediate_fill
        self.order_id = Id(f"simulator.{exchange_name.name}.order_id")
        self.trade_id = Id(f"simulator.{exchange_name.name}.trade_id")
        self.orders: dict[int, OrderTracker] = {}
        self.order_books: dict[str, OrderBook] = {}
        self.now_ts: float | None = None

        self.register(EventType.MARKET_ORDER_BOOK, self.handle_order_book)

    @property
    def exchange_name(self) -> ExchangeName:
        return self._exchange_name

    def set_event_queue(self, event_queue):
        self.event_queue = event_queue

    def subscribe_market_data_feed(self, market_feed: MarketDataFeed):
        market_feed.add_order_book_listener(self.event_queue)

    def handle_order_book(self, order_book: OrderBook):
        if order_book.market.exchange_name != self.exchange_name:
            return
        self.now_ts = order_book.get_ts()
        self.order_books[order_book.market.id] = order_book
        for order_tracker in list(self.orders.values()):
            order_request = order_tracker.order_request

            # Check if we have orderbook for this market
            if order_request.market.id not in self.order_books:
                continue

            market_price = self.order_books[order_request.market.id].market_price(order_request.size)
            if not market_price:
                continue

            if order_request.order_type == OrderType.MAKER:
                self.match_maker_order(order_tracker, market_price)
            elif order_request.order_type == OrderType.STOP:
                self.match_stop_order(order_tracker, market_price)
            else:
                self.match_taker_order(order_tracker, market_price)

    def match_maker_order(self, order_tracker: OrderTracker, market_price: MarketPrice):
        """
        Match maker (limit) order.

        Maker orders add liquidity to the orderbook. They fill when:
        - BUY: order price >= current best ask (would cross the spread)
        - SELL: order price <= current best bid (would cross the spread)

        When filled, use the order's limit price (not market price) since
        maker orders provide liquidity at their specified price.
        """
        # TODO implement partial match
        # TODO implement post_only
        order_request = order_tracker.order_request
        price_unit = get_symbol_def(order_request.market.symbol).price_unit

        if order_request.side == Side.BUY:
            # BUY maker fills when our price reaches or crosses the ask
            # Use >= to account for price tick size
            if order_request.price >= market_price.ask + price_unit:
                # Fill at our limit price (maker gets price improvement)
                self._fill_order(order_tracker, market_price.ask)
        else:
            # SELL maker fills when our price reaches or crosses the bid
            if order_request.price <= market_price.bid - price_unit:
                # Fill at our limit price (maker gets price improvement)
                self._fill_order(order_tracker, market_price.bid)

    def match_stop_order(self, order_tracker: OrderTracker, market_price: MarketPrice):
        order_request = order_tracker.order_request
        if order_request.side == Side.BUY:
            if order_request.price > market_price.ask:
                self._fill_order(order_tracker, market_price.ask)
        else:
            if order_request.price < market_price.bid:
                self._fill_order(order_tracker, market_price.bid)

    def match_taker_order(self, order_tracker: OrderTracker, market_price: MarketPrice):
        """
        Match taker (market) order with configurable slippage.

        Taker orders remove liquidity and pay slippage.
        Slippage rate is configurable via constructor parameter.
        If immediate_fill is True, fills at order's requested price (for testing).
        """
        order_request = order_tracker.order_request

        if self.immediate_fill:
            # Testing mode: fill at requested price
            exec_price = order_request.price
        else:
            # Normal mode: fill at market price + slippage
            if order_request.side == Side.BUY:
                # BUY: pay ask + slippage
                base_price = market_price.ask
                exec_price = base_price * (1 + self.slippage_rate)
            else:
                # SELL: receive bid - slippage
                base_price = market_price.bid
                exec_price = base_price * (1 - self.slippage_rate)

        self._fill_order(order_tracker, exec_price)

    def _fill_order(self, order_tracker: OrderTracker, exec_price: float):
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
        self.publish_order_filled(order_tracker.filled_response())

    def handle_create_order(self, order_request: OrderRequest):
        self.now_ts = order_request.ts
        order_id = self.order_id.next()
        order_tracker = OrderTracker(order_request, order_id, self.now_ts)
        self.orders[order_request.ctx_id] = order_tracker
        self.publish_order_created(order_tracker.created_response())

    def handle_cancel_order(self, order_request: OrderRequest):
        if order_request.ctx_id not in self.orders:
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
            return
        order_tracker = self.orders[order_request.ctx_id]
        order_tracker.set_cancelled(self.now_ts)
        del self.orders[order_request.ctx_id]
        self.publish_order_cancelled(order_tracker.cancelled_response())

    def fetch_balance_sync(self, symbol):
        return {"base": 999999999.0, "quote": 999999999.0}
