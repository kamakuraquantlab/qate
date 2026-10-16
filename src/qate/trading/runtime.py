from logging import getLogger

from qate.core.conn import Connection
from qate.core.ev_loop import EventLoop
from qate.core.ev_type import EventType
from qate.core.feed import MarketDataFeed
from qate.trading.gateways.queued import QueuedGateway
from qate.trading.reporter import Reporter
from qate.trading.strategy import Strategy
from qate.trading.trader import Trader

LOG = getLogger(__name__)


class Runtime(EventLoop):
    def __init__(self, strategy: Strategy):
        super().__init__(None, heartbeat_interval=5)
        self.trader = Trader(strategy)

        self.register(EventType.EV_LOOP_EXIT, self.handle_exit)

        self.gateways: list[QueuedGateway] = []
        self.reporters: list[Reporter] = []
        self.conns: list[Connection] = []

        self.should_exit = False

    def add_conn(self, conn: Connection):
        self.conns.append(conn)
        conn.add_status_listener(self.event_queue)

    def add_gateway(self, gateway: QueuedGateway):
        self.gateways.append(gateway)
        gateway.add_order_listener(self.event_queue)
        gateway.add_status_listener(self.event_queue)

    def add_reporter(self, reporter: Reporter):
        self.reporters.append(reporter)
        reporter.add_status_listener(self.event_queue)

        self.register(EventType.EXCEPTION, reporter.report_exception)
        self.register(EventType.ORDER_FILLED, reporter.report_order)
        self.register(EventType.METRICS, reporter.report_metrics)
        self.register(EventType.TRADING_PNL_UPDATE, reporter.report_pnl)

    def before_loop(self):
        # setup
        self.trader.add_status_listener(self.event_queue)
        for conn in self.conns:
            if isinstance(conn, MarketDataFeed):
                self.trader.subscribe_market_data_feed(conn)
        for gateway in self.gateways:
            self.trader.add_gateway(gateway)

        # start
        for reporter in self.reporters:
            LOG.info(f"reporter {reporter.__class__.__name__} start")
            reporter.start()
        if self.trader:
            LOG.info(f"trader {self.trader.__class__.__name__} start")
            self.trader.start()
        for gateway in self.gateways:
            LOG.info(f"gateway {gateway.__class__.__name__} {gateway.exchange_name.name} start")
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
        finally:
            super().stop()
