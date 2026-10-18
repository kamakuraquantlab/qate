import time
from dataclasses import dataclass
from functools import partial
from logging import getLogger

from qate.core.conn import Connection
from qate.core.ev_loop import EventLoop
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed, OrderFeed
from qate.core.gateway import ExchangeGateway
from qate.core.model import ExchangeName
from qate.core.order import OrderRequest, OrderResponse
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

LOG = getLogger(__name__)

EVENT_STOP = "RUNTIME_STOP"


ORDER_RESPONSE_EVENTS = (
    EventType.ORDER_CREATED,
    EventType.ORDER_UPDATED,
    EventType.ORDER_FILLED,
    EventType.ORDER_CANCELLED,
    EventType.ORDER_ERROR,
)


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
        order_request.set_instance_key(self.instance_key)
        self.shared_gateway.create(order_request)

    def cancel(self, order_request: OrderRequest) -> None:
        if order_request.instance_key != self.instance_key:
            raise ValueError("Trader cannot cancel an order it does not own")
        self.shared_gateway.cancel(order_request)

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
    def __init__(self, trader_stop_timeout_ts: float = 40.0, gateway_stop_timeout_ts: float = 15.0):
        # Traders get longer than a strategy's own shutdown (pisces waits 30s for
        # its cancels); gateways get longer than the async gateway's drain.
        super().__init__(None, heartbeat_interval=1)
        self.register(EventType.EV_LOOP_EXIT, self.handle_exit)
        self.register(EVENT_STOP, self.handle_stop)
        self.trader_stop_timeout_ts = trader_stop_timeout_ts
        self.gateway_stop_timeout_ts = gateway_stop_timeout_ts

        self.conns: list[Connection] = []
        self.gateways: list[ExchangeGateway] = []
        self.traders: list[Trader] = []
        self._instance_keys: dict[Trader, str] = {}
        self.reporters: list[Reporter] = []

        self.should_exit = False
        self._exited_traders: set[Trader] = set()
        self._trader_deadline: float | None = None
        self._torn_down = False

    def add_conn(self, conn: Connection):
        self.conns.append(conn)
        conn.add_status_listener(self.event_queue)

    def add_gateway(self, gateway: ExchangeGateway):
        self.gateways.append(gateway)
        gateway.add_order_listener(self.event_queue)
        if isinstance(gateway, EventLoop):
            gateway.add_status_listener(self.event_queue)

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
        self.register(EventType.TRADING_ORDER, reporter.report_order)
        self.register(EventType.TRADING_METRICS, reporter.report_metrics)
        self.register(EventType.TRADING_PNL_UPDATE, reporter.report_pnl)

    def handle_order_response(self, event_type: str, order_response: OrderResponse) -> None:
        order_request = order_response.order_request
        gateway = self._instance_gateways[(order_request.instance_key, order_request.market.exchange_name)]
        gateway.publish_order_response(event_type, order_response)

    def setup_multi(self):
        self._instance_gateways: dict[tuple[str, ExchangeName], _Gateway] = {}
        for event_type in ORDER_RESPONSE_EVENTS:
            self.register(event_type, partial(self.handle_order_response, event_type))

        for conn in self.conns:
            if isinstance(conn, MarketDataFeed):
                for trader in self.traders:
                    trader.subscribe_market_data_feed(conn)
        for shared_gateway in self.gateways:
            for trader in self.traders:
                proxy = _Gateway(shared_gateway, self, self._instance_keys[trader])
                trader.add_gateway(proxy)
                self._instance_gateways[(proxy.instance_key, proxy.exchange_name)] = proxy

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
        if isinstance(ev_loop, Trader):
            self._exited_traders.add(ev_loop)
        if not self.should_exit:
            LOG.info("Stop all because ev_loop=%s exited", ev_loop.__class__.__name__)
            self.handle_stop()
        self._tear_down_if_traders_done()

    def stop(self):
        """Ask every trader to stop, then tear down once they have.

        Callable from any thread. The shutdown runs on this loop so that it keeps
        routing order responses meanwhile: a stopping strategy is waiting for the
        confirmations of the cancels it just sent.
        """
        if self.is_alive():
            self.put(EVENT_STOP)
        else:
            self._tear_down()

    def handle_stop(self, _=None):
        if self.should_exit:
            return
        self.should_exit = True
        LOG.info("stop")
        self._trader_deadline = time.monotonic() + self.trader_stop_timeout_ts
        for trader in self.traders:
            LOG.info(f"stop trader {trader.__class__.__name__} {trader.strategy.__class__.__name__}")
            trader.stop()
        self._tear_down_if_traders_done()

    def heartbeat(self):
        super().heartbeat()
        self._tear_down_if_traders_done()

    def _tear_down_if_traders_done(self):
        if not self.should_exit or self._torn_down:
            return
        running = [t for t in self.traders if t not in self._exited_traders and t.is_alive()]
        if running and time.monotonic() < self._trader_deadline:
            return
        for trader in running:
            LOG.error(f"trader {trader.strategy.__class__.__name__} did not stop in {self.trader_stop_timeout_ts}s")
        self._tear_down()

    def _tear_down(self):
        if self._torn_down:
            return
        self._torn_down = True
        try:
            for conn in self.conns:
                LOG.info(f"disconnect conn {conn.__class__.__name__} {conn.exchange_name.name}")
                conn.disconnect()

            for gateway in self.gateways:
                LOG.info(f"stop gateway {gateway.__class__.__name__} {gateway.exchange_name.name}")
                if isinstance(gateway, EventLoop):
                    gateway.stop()
                    gateway.join(timeout=self.gateway_stop_timeout_ts)
        finally:
            super().stop()
