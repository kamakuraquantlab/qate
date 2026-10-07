from logging import getLogger

from qate.core.conn import Connection
from qate.core.ev_loop import EventLoop
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed
from qate.core.order import OrderResponse
from qate.trading.gateways.queued import QueuedGateway
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

LOG = getLogger(__name__)


class Runtime(EventLoop):
    def __init__(self, strategy: Strategy):
        super().__init__(None, heartbeat_interval=5)
        self.trader = Trader(strategy)
        self.trader.add_status_listener(self.event_queue)
        self.gateways: list[QueuedGateway] = []
        self.reporters: list[Reporter] = []

        self.register(EventType.EV_LOOP_EXIT, self.handle_exit)
        self.register(EventType.EXCEPTION, self.handle_reportable_exception)
        self.register(EventType.MSG_OUT, self.handle_outgoing_message)
        self.register(EventType.MSG_IN, self.trader.notify)
        self.register(EventType.ORDER_FILLED, self.handle_order)
        self.register(EventType.METRICS, self.handle_metrics)
        self.register(EventType.TRADING_PNL_UPDATE, self.handle_pnl_update)
        self.add_status_listener(self.event_queue)

        self.conns: list[Connection] = []
        self.should_exit = False
        self.reporters_stopped = False

    def add_conn(self, conn: Connection):
        self.conns.append(conn)
        conn.add_status_listener(self.event_queue)
        if isinstance(conn, MarketDataFeed):
            self.trader.subscribe_market_data_feed(conn)

    def add_gateway(self, gateway: QueuedGateway):
        gateway.add_order_listener(self.event_queue)
        gateway.add_status_listener(self.event_queue)
        self.gateways.append(gateway)
        self.trader.add_gateway(gateway)

    def add_reporter(self, reporter: Reporter):
        self.reporters.append(reporter)
        reporter.add_status_listener(self.event_queue)

    def _report(self, call, *args):
        for reporter in self.reporters:
            try:
                getattr(reporter, call)(*args)
            except Exception:
                LOG.exception(f"Reporter {reporter.__class__.__name__}.{call} failed")

    def before_loop(self):
        for reporter in self.reporters:
            LOG.info(f"reporter {reporter.__class__.__name__} start")
        self._report("start")
        if self.trader:
            LOG.info(f"trader {self.trader.__class__.__name__} start")
            self.trader.start()
        for gateway in self.gateways:
            LOG.info(f"gateway {gateway.__class__.__name__} {gateway.exchange_name.name} start")
            gateway.start()
        for conn in self.conns:
            conn.connect()

    def after_loop(self):
        self._stop_reporters()

    def _stop_reporters(self):
        if self.reporters_stopped:
            return
        self.reporters_stopped = True
        for reporter in self.reporters:
            LOG.info(f"stop reporter {reporter.__class__.__name__}")
        self._report("stop")

    def handle_order(self, order_response: OrderResponse):
        self._report("report_order", order_response)

    def handle_metrics(self, metrics: list):
        self._report("report_metrics", metrics)

    def handle_pnl_update(self, pnl_update: PnlUpdate):
        self._report("report_pnl", pnl_update)

    def handle_reportable_exception(self, error: BaseException):
        self._report("report_exception", error)

    def handle_outgoing_message(self, message: str):
        self._report("report_message", message)

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
            if self.trader:
                LOG.info(f"stop trader {self.trader.__class__.__name__}")
                self.trader.stop()
                self.trader.join(timeout=5.0)

            for conn in self.conns:
                LOG.info(f"disconnect conn {conn.__class__.__name__} {conn.exchange_name.name}")
                conn.disconnect()

            for gateway in self.gateways:
                LOG.info(f"stop gateway {gateway.__class__.__name__} {gateway.exchange_name.name}")
                gateway.stop()
                gateway.join(timeout=5.0)

            self._stop_reporters()

        finally:
            super().stop()
