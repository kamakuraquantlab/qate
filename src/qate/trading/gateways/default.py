import time
from dataclasses import dataclass
from logging import getLogger

from qate.core.api import Api
from qate.core.ev_type import EventType
from qate.core.feed import ExchangeFeed
from qate.core.gateway import ExchangeGateway
from qate.core.order import (
    ExchangeExecution,
    ExchangeOrder,
    OrderError,
    OrderRequest,
    OrderResponse,
    OrderState,
    OrderTracker,
)
from qate.core.symbol import Symbol

from ..exceptions import StopTradingException

LOG = getLogger(__name__)


class DefaultGateway(ExchangeGateway):
    def __init__(self, api: Api, heartbeat_interval_ts: float = 3.0):
        super(DefaultGateway, self).__init__(api, heartbeat_interval_ts)
        self.exchange_orders: dict[str, OrderTracker] = {}
        self.repeat(5, ReconOrdersCallable())

    def subscribe_exchange_feed(self, exchange_feed: ExchangeFeed):
        exchange_feed.add_execution_listener(self.event_queue)
        exchange_feed.add_order_listener(self.event_queue)
        self.register(EventType.EXCHANGE_EXECUTION, self.handle_exchange_execution)
        self.register(EventType.EXCHANGE_ORDER, self.handle_exchange_order)

    # Order creation operations
    def handle_create_order(self, order_request: OrderRequest):
        order_id = None
        try:
            order_id = self.api.create_order(order_request)
        except StopTradingException:
            raise
        except Exception as e:
            LOG.error("Failed to create order")
            LOG.exception(e)

        if order_id is None:
            self.publish_order_error(OrderResponse(order_request, error=OrderError.CREATE))
            return

        order_tracker = OrderTracker(order_request, order_id, time.time())
        self.exchange_orders[order_id] = order_tracker
        self.publish_order_created(order_tracker.created_response())

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

    # Order cancellation operations
    def handle_cancel_order(self, order_request: OrderRequest):
        order_tracker = self.exchange_orders[order_request.order_id]
        LOG.info(f"Cancel order ctx_id={order_request.ctx_id} retry={order_tracker.retry_cnt}")

        if self.api.cancel_order(order_tracker.order_request):
            order_tracker.set_cancelled(time.time())
            self.publish_order_cancelled(order_tracker.cancelled_response())
        else:
            self.publish_order_error(OrderResponse(order_request))

        del self.exchange_orders[order_request.order_id]

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


@dataclass
class CheckOrderStatusCallable:
    def __call__(self, caller: DefaultGateway):
        if len(caller.exchange_orders) == 0:
            return

        received_orders = []
        for order_tracker in caller.exchange_orders.values():
            symbol = order_tracker.order_request.market.symbol
            try:
                exchange_order = caller.api.fetch_order(order_tracker.order_id, symbol)
            except Exception as e:
                LOG.warning(f"fetch_order error, will retry: {e.__class__.__name__}: {e}")
                return
            if not exchange_order:
                continue
            if exchange_order.state == OrderState.CREATED:
                continue
            received_orders.append(exchange_order)

        for o in received_orders:
            caller.handle_exchange_order(o)


@dataclass
class CancelOrderCallable:
    order_request: OrderRequest

    def __call__(self, caller: DefaultGateway):
        caller.handle_cancel_order(self.order_request)


@dataclass
class ReconOrdersCallable:
    """
    WebSocket and long-poll connections WILL disconnect and reconnect — network blips,
    exchange-side restarts, and process restarts are not edge cases; they are certainties
    over any multi-day trading horizon. When the private connection drops, fill events
    are lost: orders stay stuck in exchange_orders forever, blocking the strategy from
    placing new ones.

    Recon is therefore not optional hardening — it is a required component of a
    self-correcting trading engine. Any order older than stale_after_ts that is still
    open in exchange_orders must be checked against the exchange's REST API so the
    gateway can catch up on events that arrived while the connection was down.
    """

    stale_after_ts: float = 10.0
    batch_size: int = 5

    def __call__(self, caller: DefaultGateway):
        if len(caller.exchange_orders) == 0:
            return
        LOG.info(f"ReconOrdersCallable {len(caller.exchange_orders)} pending orders")

        now = time.time()
        stale_orders: list[tuple[float, OrderTracker]] = []
        for order_tracker in caller.exchange_orders.values():
            if now - order_tracker.last_recon_ts < self.stale_after_ts:
                continue
            if order_tracker.state in (OrderState.CANCELLED, OrderState.FILLED):
                continue
            stale_orders.append((order_tracker.last_recon_ts, order_tracker))

        if not stale_orders:
            return

        LOG.info(f"ReconOrdersCallable processing {len(stale_orders)} orders")
        cancelled_orders = []
        executions = []
        for i, (_, order_tracker) in enumerate(stale_orders):
            if i >= self.batch_size:
                break

            symbol = order_tracker.order_request.market.symbol
            order_id = order_tracker.order_id
            exchange_order = caller.api.fetch_order(order_id, symbol)
            order_tracker.last_recon_ts = now
            if not exchange_order or exchange_order.state == OrderState.CREATED:
                continue
            if exchange_order.state == OrderState.CANCELLED:
                # Maker orders may come here. There are 2 cases:
                # 1) The order is fully cancelled, explanation is not needed.
                # 2) The order is partially filled. The gateway is waiting for full-fill to notify the strategy.
                #  The strategy is waiting for too long so it cancelled the order. Given this is a trading
                #  system for high frequency trading, we consider "partially filled and cancelled" orders are late.
                #  Don't want to react to the filled part, just ignore it as cancelled.
                cancelled_orders.append(exchange_order)
                continue

            execs = caller.api.fetch_executions(order_id, symbol)
            if execs:
                executions.extend(execs)

        for exec in executions:
            LOG.info(f"ReconOrdersCallable execution {exec}")
            caller.handle_exchange_execution(exec)

        for o in cancelled_orders:
            LOG.info(f"ReconOrdersCallable cancelled order_id={o.order_id}")
            caller.handle_exchange_order(o)
