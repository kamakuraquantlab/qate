from dataclasses import dataclass
from functools import partial
from logging import getLogger

from qate.core.conn import Connection
from qate.core.ev_loop import EventLoop
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed, OrderFeed
from qate.core.gateway import ExchangeGateway
from qate.core.order import OrderRequest, OrderResponse
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

LOG = getLogger(__name__)

EVENT_CREATE_ORDER = "RUNTIME_CREATE_ORDER"
EVENT_CANCEL_ORDER = "RUNTIME_CANCEL_ORDER"
EVENT_INSTANCE_ORDER_FILLED = "RUNTIME_INSTANCE_ORDER_FILLED"

ORDER_RESPONSE_EVENTS = (
    EventType.ORDER_CREATED,
    EventType.ORDER_UPDATED,
    EventType.ORDER_FILLED,
    EventType.ORDER_CANCELLED,
    EventType.ORDER_ERROR,
)
TERMINAL_ORDER_EVENTS = frozenset((EventType.ORDER_FILLED, EventType.ORDER_CANCELLED, EventType.ORDER_ERROR))


@dataclass(frozen=True)
class StrategyEvent:
    """An outcome attributed to one strategy instance in a shared runtime."""

    instance_key: str | None
    value: object


class _StatusSink:
    def __init__(self, runtime: "Runtime2", instance_key: str | None):
        self.runtime = runtime
        self.instance_key = instance_key

    def put(self, item):
        if not item:
            self.runtime.event_queue.put(item)
            return
        event_type, event = item
        if event_type == EventType.EV_LOOP_EXIT:
            self.runtime.event_queue.put(item)
            return
        self.runtime.event_queue.put((event_type, StrategyEvent(self.instance_key, event)))


class _Gateway(ExchangeGateway):
    """One trader's view of a gateway shared by several traders."""

    def __init__(self, gateway: ExchangeGateway, runtime: "Runtime2", instance_key: str):
        OrderFeed.__init__(self)
        self.shared_gateway = gateway
        self.runtime = runtime
        self.instance_key = instance_key

    @property
    def exchange_name(self):
        return self.shared_gateway.exchange_name

    def create(self, order_request: OrderRequest) -> None:
        self.runtime.put(EVENT_CREATE_ORDER, (self, order_request))

    def cancel(self, order_request: OrderRequest) -> None:
        self.runtime.put(EVENT_CANCEL_ORDER, (self, order_request))

    def __getattr__(self, name):
        # Adapters may add synchronous capabilities such as fetch_balance_sync.
        return getattr(self.shared_gateway, name)

    def publish_order_response(self, event_type: str, order_response: OrderResponse) -> None:
        if event_type == EventType.ORDER_CREATED:
            self.publish_order_created(order_response)
        elif event_type == EventType.ORDER_UPDATED:
            self.publish_order_updated(order_response)
        elif event_type == EventType.ORDER_FILLED:
            self.publish_order_filled(order_response)
        elif event_type == EventType.ORDER_CANCELLED:
            self.publish_order_cancelled(order_response)
        elif event_type == EventType.ORDER_ERROR:
            self.publish_order_error(order_response)


class Runtime2(EventLoop):
    def __init__(self):
        super().__init__(None, heartbeat_interval=5)
        self.register(EventType.EV_LOOP_EXIT, self.handle_exit)

        self.conns: list[Connection] = []
        self.gateways: list[ExchangeGateway] = []
        self.traders: list[Trader] = []
        self._instance_keys: dict[Trader, str] = {}
        self.reporters: list[Reporter] = []

        self.should_exit = False

    def add_conn(self, conn: Connection):
        self.conns.append(conn)
        conn.add_status_listener(_StatusSink(self, None))

    def add_gateway(self, gateway: ExchangeGateway):
        self.gateways.append(gateway)
        gateway.add_order_listener(self.event_queue)
        if isinstance(gateway, EventLoop):
            gateway.add_status_listener(_StatusSink(self, None))

    def add_strategy(self, strategy: Strategy, instance_key: str | None = None):
        instance_key = instance_key or f"{strategy.__class__.__module__}.{strategy.__class__.__name__}"
        if instance_key in self._instance_keys.values():
            raise ValueError(f"Duplicate strategy instance key: {instance_key}")
        trader = Trader(strategy, reload_params=False)
        self.traders.append(trader)
        self._instance_keys[trader] = instance_key
        trader.add_status_listener(_StatusSink(self, instance_key))

    def add_reporter(self, reporter: Reporter):
        self.reporters.append(reporter)

        self.register(EventType.EXCEPTION, reporter.report_exception)
        self.register(EVENT_INSTANCE_ORDER_FILLED, reporter.report_order)
        self.register(EventType.METRICS, reporter.report_metrics)
        self.register(EventType.TRADING_PNL_UPDATE, reporter.report_pnl)

    def handle_create_order(self, event: tuple[_Gateway, OrderRequest]) -> None:
        gateway, order_request = event
        request_key = id(order_request)
        if request_key in self._order_owners:
            raise ValueError("Order request has already been submitted")
        self._order_owners[request_key] = gateway
        try:
            gateway.shared_gateway.create(order_request)
        except BaseException:
            del self._order_owners[request_key]
            raise

    def handle_cancel_order(self, event: tuple[_Gateway, OrderRequest]) -> None:
        gateway, order_request = event
        if self._order_owners[id(order_request)] is not gateway:
            raise ValueError("Trader cannot cancel an order it does not own")
        gateway.shared_gateway.cancel(order_request)

    def handle_order_response(self, event_type: str, order_response: OrderResponse) -> None:
        request_key = id(order_response.order_request)
        gateway = self._order_owners[request_key]
        gateway.publish_order_response(event_type, order_response)
        if event_type == EventType.ORDER_FILLED:
            self.put(EVENT_INSTANCE_ORDER_FILLED, StrategyEvent(gateway.instance_key, order_response))
        if event_type in TERMINAL_ORDER_EVENTS:
            del self._order_owners[request_key]

    def setup_multi(self):
        # enable order routing
        self._order_owners: dict[int, _Gateway] = {}
        self.register(EVENT_CREATE_ORDER, self.handle_create_order)
        self.register(EVENT_CANCEL_ORDER, self.handle_cancel_order)
        for event_type in ORDER_RESPONSE_EVENTS:
            self.register(event_type, partial(self.handle_order_response, event_type))

        for conn in self.conns:
            if isinstance(conn, MarketDataFeed):
                for trader in self.traders:
                    trader.subscribe_market_data_feed(conn)
        for gateway in self.gateways:
            for trader in self.traders:
                trader.add_gateway(_Gateway(gateway, self, self._instance_keys[trader]))

    def before_loop(self):
        # setup
        if self.traders:
            self.setup_multi()

        # start
        for reporter in self.reporters:
            LOG.info(f"reporter {reporter.__class__.__name__} start")
            reporter.start()
        for trader in self.traders:
            LOG.info(f"trader {trader.__class__.__name__} {trader.strategy.__class__.__name__} start")
            trader.start()
        for gateway in self.gateways:
            LOG.info(f"gateway {gateway.__class__.__name__} {gateway.exchange_name.name} start")
            if isinstance(gateway, EventLoop):
                gateway.start()
        for conn in self.conns:
            conn.connect()

    def after_loop(self):
        for reporter in self.reporters:
            LOG.info(f"stop reporter {reporter.__class__.__name__}")
            reporter.stop()

    def handle_exit(self, ev_loop: EventLoop):
        if self.should_exit:
            return
        LOG.info("Stop all because ev_loop=%s exited", ev_loop.__class__.__name__)
        self.stop()

    def stop(self):
        if self.should_exit:
            return
        self.should_exit = True
        LOG.info("stop")
        try:
            for trader in self.traders:
                LOG.info(f"stop trader {trader.__class__.__name__} {trader.strategy.__class__.__name__}")
                trader.stop()
                trader.join(timeout=5.0)

            for conn in self.conns:
                LOG.info(f"disconnect conn {conn.__class__.__name__} {conn.exchange_name.name}")
                conn.disconnect()

            for gateway in self.gateways:
                LOG.info(f"stop gateway {gateway.__class__.__name__} {gateway.exchange_name.name}")
                if isinstance(gateway, EventLoop):
                    gateway.stop()
                    gateway.join(timeout=5.0)
        finally:
            super().stop()
