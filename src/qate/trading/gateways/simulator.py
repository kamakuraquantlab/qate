from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed
from qate.core.model import ExchangeName, OrderBook
from qate.core.order import OrderRequest

from .backtest import OrderMatching, _log_completed_cancel
from .queued import QueuedGateway


class SimulatorGateway(QueuedGateway):
    """Paper trading against a live feed, with simulated fills.

    Books arrive on the feed's thread and orders on each trader's. Both are queued
    and handled on this gateway's own loop, so `OrderMatching` is only ever called
    from one thread, as it would be in a backtest. A replay uses `BacktestGateway`,
    which has no loop to drain.
    """

    def __init__(
        self,
        exchange_name: ExchangeName,
        slippage_rate: float = 0.0,
        immediate_fill: bool = False,
        heartbeat_interval_ts: float | None = None,
    ):
        super().__init__(None, heartbeat_interval_ts)
        self.matching = OrderMatching(exchange_name, slippage_rate, immediate_fill)
        self.register(EventType.MARKET_ORDER_BOOK, self.handle_order_book)

    @property
    def exchange_name(self) -> ExchangeName:
        return self.matching.exchange_name

    def subscribe_market_data_feed(self, market_feed: MarketDataFeed):
        market_feed.add_order_book_listener(self.event_queue)

    def handle_order_book(self, order_book: OrderBook):
        for order_tracker in self.matching.match(order_book):
            self.publish_order_filled(order_tracker.filled_response())

    def handle_create_order(self, order_request: OrderRequest):
        order_tracker = self.matching.create(order_request)
        self.publish_order_created(order_tracker.created_response())

    def handle_cancel_order(self, order_request: OrderRequest):
        order_tracker = self.matching.cancel(order_request)
        if order_tracker is None:
            _log_completed_cancel(order_request)
            return
        self.publish_order_cancelled(order_tracker.cancelled_response())

    def fetch_balance_sync(self, symbol):
        return {"base": 999999999.0, "quote": 999999999.0}
