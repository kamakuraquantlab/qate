from logging import getLogger

from qate.core.api import Api
from qate.core.ev_type import EventType
from qate.core.feed import ExchangeFeed
from qate.core.order import ExchangeExecution, ExchangeOrder, OrderState, OrderTracker
from qate.core.symbol import Symbol

from .queued import QueuedGateway

LOG = getLogger(__name__)


class OrderTrackingGateway(QueuedGateway):
    """The half of a live gateway that does not depend on how orders are sent.

    It holds the venue's open orders in `exchange_orders` and turns the private
    feed's executions and order updates into filled and cancelled responses. How an
    order gets into `exchange_orders` -- a blocking call on the loop's thread, or an
    async one off it -- and how stale orders are reconciled, is a subclass's:
    `DefaultGateway` and `DefaultGatewayAsync`.
    """

    def __init__(self, api: Api, heartbeat_interval_ts: float = 3.0):
        super().__init__(api, heartbeat_interval_ts)
        self.exchange_orders: dict[str, OrderTracker] = {}

    def subscribe_exchange_feed(self, exchange_feed: ExchangeFeed):
        exchange_feed.add_execution_listener(self.event_queue)
        exchange_feed.add_order_listener(self.event_queue)
        self.register(EventType.EXCHANGE_EXECUTION, self.handle_exchange_execution)
        self.register(EventType.EXCHANGE_ORDER, self.handle_exchange_order)

    def _find_order_tracker(self, execution: ExchangeExecution) -> OrderTracker:
        return self.exchange_orders.get(execution.order_id, None)

    def handle_exchange_execution(self, execution: ExchangeExecution):
        order_tracker = self._find_order_tracker(execution)
        if not order_tracker:
            # Same as handle_exchange_order, please read the comment there
            # LOG.warning(f"Received exchange execution for unknown order_id {execution}")
            return
        LOG.info(
            f"ON_EXECUTION ctx_id={order_tracker.order_request.ctx_id} "
            f"trade_id={execution.trade_id} size={execution.size:.3f}"
        )
        order_tracker.on_execution(execution)
        if order_tracker.is_filled():
            order_tracker.set_filled(execution.get_ts())
            self.publish_order_filled(order_tracker.filled_response())
            del self.exchange_orders[order_tracker.order_id]

    def handle_exchange_order(self, exchange_order: ExchangeOrder):
        order_id = exchange_order.order_id
        if order_id not in self.exchange_orders:
            return

        order_tracker = self.exchange_orders[order_id]
        LOG.info(
            f"ON_ORDER ctx_id={order_tracker.order_request.ctx_id} "
            f"state={exchange_order.state} size={exchange_order.exec_size:.3f}"
        )
        order_tracker.on_exchange_order(exchange_order)
        if exchange_order.state == OrderState.FILLED:
            self.publish_order_filled(order_tracker.filled_response())
            del self.exchange_orders[order_id]
        elif exchange_order.state == OrderState.CANCELLED:
            self.publish_order_cancelled(order_tracker.cancelled_response())
            del self.exchange_orders[order_id]

    def fetch_balance_sync(self, symbol: Symbol):
        return self.api.fetch_balance(symbol)
